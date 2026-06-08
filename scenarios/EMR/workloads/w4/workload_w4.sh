#!/usr/bin/env bash
# W-4 -- filter-boundary churn.  50% writes on the filter-IN prefix, 50% on filter-OUT.
# RP-2 phase (b) uses this -- IN_PREFIX matches the current sink filter, OUT_PREFIX doesn't.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"
: "${IN_PREFIX:?}" "${OUT_PREFIX:?}" "${POOL_SIZE:?}"

duration="${DURATION_SECS:-0}"
writer="${WRITER_ID:-}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w4-${TARGET}-${DB_NAME}${writer:+-w${writer}}.pid"
logfile="/tmp/w4-${TARGET}-${DB_NAME}${writer:+-w${writer}}.log"

setup_pidfile() {
  echo $$ > "$pidfile"
  trap 'rm -f "$pidfile"' EXIT
}

put_doc() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/docs?id=$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null || echo 000
}

is_put_ok() { [ "$1" = 200 ] || [ "$1" = 201 ]; }

ok=0; err=0; in_puts=0; out_puts=0

# Pick a side 50/50.  Sets `side_prefix`.
pick_side() {
  if [ $((RANDOM % 2)) -eq 0 ]; then
    side_prefix="$IN_PREFIX"
    in_puts=$((in_puts+1))
  else
    side_prefix="$OUT_PREFIX"
    out_puts=$((out_puts+1))
  fi
}

do_op() {
  pick_side
  local n=$((RANDOM % POOL_SIZE))
  local body="{\"v\":$((ok+err+1)),\"src\":\"w4${writer:+-$writer}\",\"@metadata\":{\"@collection\":\"FilterBoundary\"}}"
  if is_put_ok "$(put_doc "${side_prefix}/${n}" "$body")"; then ok=$((ok+1)); else err=$((err+1)); fi
}

run_loop() {
  local deadline=0
  [ "$duration" -gt 0 ] && deadline=$(( $(date +%s) + duration ))
  while true; do
    [ "$deadline" -gt 0 ] && [ "$(date +%s)" -ge "$deadline" ] && break
    do_op
  done
}

# ---------- main ----------

setup_pidfile
start=$(date +%s)
echo "W-4 starting  ${TARGET}/${DB_NAME}  in=${IN_PREFIX}  out=${OUT_PREFIX}  pool=${POOL_SIZE}  duration=${duration}s" | tee -a "$logfile" >&2
run_loop
elapsed=$(( $(date +%s) - start ))
echo "W-4 DONE  ${TARGET}/${DB_NAME}  ops=$((ok+err))  ok=$ok  err=$err  in=$in_puts  out=$out_puts  elapsed=${elapsed}s" | tee -a "$logfile" >&2
