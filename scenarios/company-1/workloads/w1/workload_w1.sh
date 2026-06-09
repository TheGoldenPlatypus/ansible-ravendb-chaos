#!/usr/bin/env bash
# W-1 -- doc CRUD churn (70% update / 20% put-new / 10% delete) on a seeded pool.
# Single-bucket mode:  ID_PREFIX + POOL_SIZE.
# Multi-bucket mode:   BUCKETS_SPEC="prefix:pool:weight|prefix:pool:weight|..."

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"

duration="${DURATION_SECS:-0}"
writer="${WRITER_ID:-}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w1-${TARGET}-${DB_NAME}${writer:+-w${writer}}.pid"
debuglog="/tmp/w1-${TARGET}-${DB_NAME}${writer:+-w${writer}}.debug.log"

# ---------- pidfile + death-trace ----------
# When W-1 dies we need to know WHY -- "pidfile vanished" is not enough.  Record the
# signal that killed us (or natural exit code) + a tail of recent loop activity into
# a persistent log file the harness can dump when assert_workload_alive fires.

log() { echo "$(date '+%H:%M:%S.%3N')  $*" >> "$debuglog"; }

on_exit() {
  local rc=$?
  log "EXIT  rc=$rc  updates=$updates puts=$puts deletes=$deletes errs=$errs  last_op=${last_op:-none}  last_http=${last_http:-none}"
  rm -f "$pidfile"
}
on_signal() {
  local sig=$1
  log "SIGNAL  $sig  (will exit after trap)"
}

setup_pidfile() {
  echo $$ > "$pidfile"
  : > "$debuglog"
  log "STARTED  pid=$$  target=$TARGET  db=$DB_NAME  writer=${writer:-<none>}  pidfile=$pidfile"
  trap on_exit EXIT
  trap 'on_signal SIGTERM' TERM
  trap 'on_signal SIGHUP'  HUP
  trap 'on_signal SIGINT'  INT
  trap 'on_signal SIGPIPE' PIPE
  trap 'on_signal SIGUSR1' USR1
}

# Running counters / last-op state -- referenced in on_exit so declare early.
updates=0; puts=0; deletes=0; errs=0; last_op=""; last_http=""

# ---------- buckets ----------
# Parallel arrays.  pick_bucket sets bucket_prefix + bucket_pool weighted by cum_weights.

prefixes=(); pools=(); cum_weights=(); total_weight=0

add_bucket() {
  prefixes+=("$1")
  pools+=("$2")
  total_weight=$(( total_weight + $3 ))
  cum_weights+=("$total_weight")
}

setup_buckets() {
  if [ -n "${BUCKETS_SPEC:-}" ]; then
    local entry p pool w
    IFS='|' read -ra entries <<< "$BUCKETS_SPEC"
    for entry in "${entries[@]}"; do
      IFS=':' read -r p pool w <<< "$entry"
      add_bucket "$p" "$pool" "$w"
    done
  else
    : "${ID_PREFIX:?}" "${POOL_SIZE:?}"
    add_bucket "$ID_PREFIX" "$POOL_SIZE" 1
  fi
}

pick_bucket() {
  local r=$((RANDOM % total_weight)) i
  for ((i=0; i<${#cum_weights[@]}; i++)); do
    if [ "$r" -lt "${cum_weights[$i]}" ]; then
      bucket_prefix="${prefixes[$i]}"
      bucket_pool="${pools[$i]}"
      return
    fi
  done
}

# ---------- HTTP primitives ----------

# curl bounded by --connect-timeout + --max-time so a target node going through
# container-restart (~20-30s window) can't hang the loop indefinitely.  Each request
# fails fast; errs counter ticks up; loop survives the chaos window.
put_doc() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' \
    --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/docs?id=$1" -H 'Content-Type: application/json' -d "$2" || echo 000
}

delete_doc() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' \
    --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X DELETE "$url/docs?id=$1" || echo 000
}

is_put_ok()    { [ "$1" = 200 ] || [ "$1" = 201 ]; }
is_delete_ok() { [ "$1" = 204 ] || [ "$1" = 404 ]; }

# ---------- ops ----------

do_update() {
  local id="${bucket_prefix}/$((RANDOM % bucket_pool))"
  local body="{\"v\":\"u-$RANDOM\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
  last_op="update $id"
  last_http=$(put_doc "$id" "$body")
  if is_put_ok "$last_http"; then updates=$((updates+1)); else errs=$((errs+1)); fi
}

do_put_new() {
  local id="${bucket_prefix}/new-$$-$RANDOM"
  local body="{\"v\":\"new\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
  last_op="put $id"
  last_http=$(put_doc "$id" "$body")
  if is_put_ok "$last_http"; then puts=$((puts+1)); else errs=$((errs+1)); fi
}

do_delete() {
  local id="${bucket_prefix}/$((RANDOM % bucket_pool))"
  last_op="delete $id"
  last_http=$(delete_doc "$id")
  if is_delete_ok "$last_http"; then deletes=$((deletes+1)); else errs=$((errs+1)); fi
}

# ---------- main loop ----------

run_loop() {
  local deadline=0
  local iter=0
  [ "$duration" -gt 0 ] && deadline=$(( $(date +%s) + duration ))

  while true; do
    [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ] && break
    pick_bucket
    local r=$((RANDOM % 100))
    if   [ "$r" -lt 70 ]; then do_update
    elif [ "$r" -lt 90 ]; then do_put_new
    else                       do_delete
    fi
    iter=$((iter+1))
    # Heartbeat every 25 iterations (~1 min wall) so the debug log shows we're alive.
    [ $((iter % 25)) -eq 0 ] && log "HEARTBEAT  iter=$iter  updates=$updates puts=$puts deletes=$deletes errs=$errs  last_http=$last_http"
    sleep 2.4    # ~25 ops/min per the RPV-1 spec
  done
}

# ---------- main ----------

setup_pidfile
setup_buckets
echo "W-1 starting  ${TARGET}/${DB_NAME}  buckets=${#prefixes[@]}  duration=${duration}s${writer:+  writer=$writer}"
run_loop
echo "W-1 DONE  ${TARGET}/${DB_NAME}  updates=$updates  puts=$puts  deletes=$deletes  errors=$errs"
