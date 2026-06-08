#!/usr/bin/env bash
# W-2 -- doc-extension churn (no doc CRUD; that's W-1's job).  Per spec:
#   25% attachment-add  / 25% attachment-remove
#   12.5% counter-inc   / 12.5% counter-dec
#   12.5% TS-append     / 12.5% TS-delete-range
# Bucket modes same as W-1.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"

duration="${DURATION_SECS:-0}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w2-${TARGET}-${DB_NAME}.pid"

# ---------- pidfile ----------

setup_pidfile() {
  echo $$ > "$pidfile"
  trap 'rm -f "$pidfile"' EXIT
}

# ---------- buckets ----------

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

# ---------- helpers ----------
# URL-encode '/' so docId query strings don't break on ids with slashes.
encode_id() { printf %s "$1" | sed 's|/|%2F|g'; }

is_ok_2xx()   { [ "$1" = 200 ] || [ "$1" = 201 ] || [ "$1" = 204 ]; }
is_delete_ok() { [ "$1" = 204 ] || [ "$1" = 404 ]; }

put_attachment() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/attachments?id=$(encode_id "$1")&name=$2" \
    -H 'Content-Type: application/octet-stream' --data-raw "$3"
}

delete_attachment() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X DELETE "$url/attachments?id=$(encode_id "$1")&name=$2"
}

post_counter() {
  local id="$1" delta="$2"
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X POST "$url/counters" -H 'Content-Type: application/json' \
    --data-raw "{\"Documents\":[{\"DocumentId\":\"$id\",\"Operations\":[{\"Type\":\"Increment\",\"CounterName\":\"Likes\",\"Delta\":$delta}]}]}"
}

post_timeseries_append() {
  local id="$1" ts="$2" value="$3"
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X POST "$url/timeseries?docId=$(encode_id "$id")" -H 'Content-Type: application/json' \
    --data-raw "{\"Name\":\"Heartrate\",\"Appends\":[{\"Timestamp\":\"$ts\",\"Values\":[$value],\"Tag\":null}]}"
}

post_timeseries_delete() {
  local id="$1" from="$2" to="$3"
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X POST "$url/timeseries?docId=$(encode_id "$id")" -H 'Content-Type: application/json' \
    --data-raw "{\"Name\":\"Heartrate\",\"Deletes\":[{\"From\":\"$from\",\"To\":\"$to\"}]}"
}

# ---------- ops ----------

att_add=0; att_rm=0; ctr_inc=0; ctr_dec=0; ts_app=0; ts_del=0; errs=0

do_att_add() {
  if is_ok_2xx "$(put_attachment "$1" "att-w2-$RANDOM" "w2-payload-$RANDOM")"; then att_add=$((att_add+1)); else errs=$((errs+1)); fi
}

do_att_rm() {
  if is_delete_ok "$(delete_attachment "$1" "att-w2-$((RANDOM % 1000))")"; then att_rm=$((att_rm+1)); else errs=$((errs+1)); fi
}

do_ctr_inc() {
  if is_ok_2xx "$(post_counter "$1"  1)"; then ctr_inc=$((ctr_inc+1)); else errs=$((errs+1)); fi
}

do_ctr_dec() {
  if is_ok_2xx "$(post_counter "$1" -1)"; then ctr_dec=$((ctr_dec+1)); else errs=$((errs+1)); fi
}

do_ts_app() {
  local ts; ts=$(date -u -d "$((RANDOM % 3600)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
  if is_ok_2xx "$(post_timeseries_append "$1" "$ts" "$((RANDOM % 100))")"; then ts_app=$((ts_app+1)); else errs=$((errs+1)); fi
}

do_ts_del() {
  local from to
  from=$(date -u -d "$((1800 + RANDOM % 3600)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
  to=$(date -u -d   "$((900  + RANDOM % 1800)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
  if is_ok_2xx "$(post_timeseries_delete "$1" "$from" "$to")"; then ts_del=$((ts_del+1)); else errs=$((errs+1)); fi
}

# ---------- main loop ----------

run_loop() {
  local deadline=0
  [ "$duration" -gt 0 ] && deadline=$(( $(date +%s) + duration ))

  while true; do
    [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ] && break
    pick_bucket
    local id="${bucket_prefix}/$((RANDOM % bucket_pool))"
    local r=$((RANDOM % 100))
    if   [ "$r" -lt 25 ]; then do_att_add "$id"
    elif [ "$r" -lt 50 ]; then do_att_rm  "$id"
    elif [ "$r" -lt 62 ]; then do_ctr_inc "$id"
    elif [ "$r" -lt 75 ]; then do_ctr_dec "$id"
    elif [ "$r" -lt 87 ]; then do_ts_app  "$id"
    else                       do_ts_del  "$id"
    fi
    sleep 2.4    # ~25 ops/min per the RPV-1 spec
  done
}

# ---------- main ----------

setup_pidfile
setup_buckets
echo "W-2 starting  ${TARGET}/${DB_NAME}  buckets=${#prefixes[@]}  duration=${duration}s"
run_loop
echo "W-2 DONE  ${TARGET}/${DB_NAME}  att_add=$att_add  att_rm=$att_rm  ctr_inc=$ctr_inc  ctr_dec=$ctr_dec  ts_app=$ts_app  ts_del=$ts_del  errors=$errs"
