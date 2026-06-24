#!/usr/bin/env bash
# W-1C -- doc CRUD churn (60% put-new / 30% update / 10% delete) on a seeded pool,
# committed via RavenDB CLUSTER-WIDE TRANSACTIONS (one op per tx, Raft-committed).
# Sidecar to W-1.  Uses the same ID prefix(es) / pool(s) so it shares W-1's doc pool;
# expect CompareExchange conflicts (HTTP 409) when W-1 and W-1C race -- counted in
# a separate `conflicts` counter rather than `errs`.
#
# Single-bucket mode:  ID_PREFIX + POOL_SIZE.
# Multi-bucket mode:   BUCKETS_SPEC="prefix:pool:weight|prefix:pool:weight|..."
#
# Endpoint: POST /databases/<db>/bulk_docs with TransactionMode=ClusterWide.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"

duration="${DURATION_SECS:-0}"
writer="${WRITER_ID:-}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w1c-${TARGET}-${DB_NAME}${writer:+-w${writer}}.pid"
debuglog="/tmp/w1c-${TARGET}-${DB_NAME}${writer:+-w${writer}}.debug.log"

# ---------- pidfile + death-trace ----------
# Mirrors W-1's death-trace pattern.  Adds `conflicts` to the exit log so a high
# CompareExchange-conflict rate is visible post-mortem.

log() { echo "$(date '+%H:%M:%S.%3N')  $*" >> "$debuglog"; }

on_exit() {
  local rc=$?
  log "EXIT  rc=$rc  updates=$updates puts=$puts deletes=$deletes conflicts=$conflicts errs=$errs  last_op=${last_op:-none}  last_http=${last_http:-none}"
  rm -f "$pidfile"
}
on_signal() {
  local sig=$1
  log "SIGNAL  $sig  (will exit after trap)"
  exit 0
}

setup_pidfile() {
  echo $$ > "$pidfile"
  : > "$debuglog"
  log "STARTED  pid=$$  target=$TARGET  db=$DB_NAME  writer=${writer:-<none>}  pidfile=$pidfile  mode=ClusterWide"
  trap on_exit EXIT
  trap 'on_signal SIGTERM' TERM
  trap 'on_signal SIGHUP'  HUP
  trap 'on_signal SIGINT'  INT
  trap 'on_signal SIGPIPE' PIPE
  trap 'on_signal SIGUSR1' USR1
}

# Running counters / last-op state -- referenced in on_exit so declare early.
# `conflicts` tracks HTTP 409 (CompareExchange conflict, expected when W-1 + W-1C race).
updates=0; puts=0; deletes=0; conflicts=0; errs=0; last_op=""; last_http=""

# ---------- buckets ----------
# Identical to W-1's bucket logic so W-1C shares the same ID pool semantics.

prefixes=(); pools=(); collections=(); cum_weights=(); total_weight=0

derive_collection() {
  case "$1" in
    users/*|users)        echo "Users" ;;
    orders/*|orders)      echo "Orders" ;;
    Internal/*|Internal)  echo "Internal" ;;
    *)                    echo "MicroDocs" ;;
  esac
}

add_bucket() {
  prefixes+=("$1")
  pools+=("$2")
  total_weight=$(( total_weight + $3 ))
  cum_weights+=("$total_weight")
  local coll="${4:-}"
  [ -n "$coll" ] || coll=$(derive_collection "$1")
  collections+=("$coll")
}

setup_buckets() {
  if [ -n "${BUCKETS_SPEC:-}" ]; then
    local entry p pool w c
    IFS='|' read -ra entries <<< "$BUCKETS_SPEC"
    for entry in "${entries[@]}"; do
      IFS=':' read -r p pool w c <<< "$entry"
      add_bucket "$p" "$pool" "$w" "${c:-}"
    done
  else
    : "${ID_PREFIX:?}" "${POOL_SIZE:?}"
    add_bucket "$ID_PREFIX" "$POOL_SIZE" 1 "${COLLECTION:-}"
  fi
}

pick_bucket() {
  local r=$((RANDOM % total_weight)) i
  for ((i=0; i<${#cum_weights[@]}; i++)); do
    if [ "$r" -lt "${cum_weights[$i]}" ]; then
      bucket_prefix="${prefixes[$i]}"
      bucket_pool="${pools[$i]}"
      bucket_collection="${collections[$i]}"
      return
    fi
  done
}

# ---------- HTTP primitive: single-op cluster-wide tx ----------
#
# RavenDB cluster-wide tx commits go through /bulk_docs with TransactionMode=ClusterWide.
# Each call wraps EXACTLY ONE command per the spec -- max Raft load.
#
# HTTP semantics:
#   200/201 -- committed
#   409     -- CompareExchange conflict (expected when W-1 + W-1C race on same key)
#   404     -- delete of nonexistent doc (treated as ok for delete, error for update)
#   other   -- failure
#
# curl is bounded so a leader-election window (~5-30s) doesn't hang the loop.

# $1 = JSON body
bulk_docs_post() {
  curl --connect-timeout 5 --max-time 20 -sk -o /dev/null -w '%{http_code}' \
    --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X POST "$url/bulk_docs" -H 'Content-Type: application/json' -H 'Raven-Client-Version: 7.2.0.0' -d "$1" || echo 000
}

# Builds a single-PUT cluster-wide tx body.
# $1 = doc id, $2 = doc JSON (the Document value)
cw_put_body() {
  printf '{"Commands":[{"Type":"PUT","Id":"%s","Document":%s,"ChangeVector":null}],"TransactionMode":"ClusterWide"}' \
    "$1" "$2"
}

# Builds a single-DELETE cluster-wide tx body.
# $1 = doc id
cw_delete_body() {
  printf '{"Commands":[{"Type":"DELETE","Id":"%s","ChangeVector":null}],"TransactionMode":"ClusterWide"}' \
    "$1"
}

is_ok_put()    { [ "$1" = 200 ] || [ "$1" = 201 ]; }
is_ok_delete() { [ "$1" = 200 ] || [ "$1" = 201 ] || [ "$1" = 204 ] || [ "$1" = 404 ]; }
is_conflict()  { [ "$1" = 409 ]; }

# ---------- ops ----------

do_update() {
  local id="${bucket_prefix}/$((RANDOM % bucket_pool))"
  local body
  body="{\"v\":\"uc-$RANDOM\",\"@metadata\":{\"@collection\":\"${bucket_collection}\"}}"
  last_op="cw-update $id"
  last_http=$(bulk_docs_post "$(cw_put_body "$id" "$body")")
  if   is_ok_put "$last_http";  then updates=$((updates+1))
  elif is_conflict "$last_http"; then conflicts=$((conflicts+1))
  else errs=$((errs+1)); fi
}

do_put_new() {
  local id="${bucket_prefix}/cwnew-$$-$RANDOM"
  local body="{\"v\":\"new-cw\",\"@metadata\":{\"@collection\":\"${bucket_collection}\"}}"
  last_op="cw-put $id"
  last_http=$(bulk_docs_post "$(cw_put_body "$id" "$body")")
  if   is_ok_put "$last_http";  then puts=$((puts+1))
  elif is_conflict "$last_http"; then conflicts=$((conflicts+1))
  else errs=$((errs+1)); fi
}

do_delete() {
  local id="${bucket_prefix}/$((RANDOM % bucket_pool))"
  last_op="cw-delete $id"
  last_http=$(bulk_docs_post "$(cw_delete_body "$id")")
  if   is_ok_delete "$last_http"; then deletes=$((deletes+1))
  elif is_conflict "$last_http"; then conflicts=$((conflicts+1))
  else errs=$((errs+1)); fi
}

# ---------- main loop ----------
#
# Mix per Karmel: 60% put-new / 30% update / 10% delete.
# Sleep cadence matches W-1's 2.4s (~25 ops/min) so a 1:1-mirrored W-1C doesn't
# overpower the baseline -- they share doc pools and we want comparable pressure.

run_loop() {
  local deadline=0
  local iter=0
  [ "$duration" -gt 0 ] && deadline=$(( $(date +%s) + duration ))

  while true; do
    [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ] && break
    pick_bucket
    local r=$((RANDOM % 100))
    if   [ "$r" -lt 60 ]; then do_put_new
    elif [ "$r" -lt 90 ]; then do_update
    else                       do_delete
    fi
    iter=$((iter+1))
    [ $((iter % 25)) -eq 0 ] && log "HEARTBEAT  iter=$iter  updates=$updates puts=$puts deletes=$deletes conflicts=$conflicts errs=$errs  last_http=$last_http"
    sleep 2.4
  done
}

# ---------- main ----------

setup_pidfile
setup_buckets
echo "W-1C starting  ${TARGET}/${DB_NAME}  buckets=${#prefixes[@]}  duration=${duration}s${writer:+  writer=$writer}  mode=ClusterWide"
run_loop
echo "W-1C DONE  ${TARGET}/${DB_NAME}  updates=$updates  puts=$puts  deletes=$deletes  conflicts=$conflicts  errors=$errs"
