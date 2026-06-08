#!/usr/bin/env bash
# W-5 -- conflict generator.  ONE side of a split-brain pair.  Launch two instances (one per
# partitioned side, same DB_NAME / ID_PREFIX / POOL_SIZE, different SIDE_LABEL) so heal
# produces conflicts.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"
: "${ID_PREFIX:?}" "${POOL_SIZE:?}" "${SIDE_LABEL:?}"

duration="${DURATION_SECS:-0}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w5-${TARGET}-${DB_NAME}-${SIDE_LABEL}.pid"
logfile="/tmp/w5-${TARGET}-${DB_NAME}-${SIDE_LABEL}.log"

setup_pidfile() {
  echo $$ > "$pidfile"
  trap 'rm -f "$pidfile"' EXIT
}

put_doc() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/docs?id=$1" -H 'Content-Type: application/json' -d "$2" 2>/dev/null || echo 000
}

is_put_ok() { [ "$1" = 200 ] || [ "$1" = 201 ]; }

ok=0; err=0

# Side-distinct body content -- the SIDE_LABEL tag in the body guarantees the two writers'
# bodies are not byte-equal, so Raven can't dedupe them and they conflict on heal.
do_op() {
  local n=$((RANDOM % POOL_SIZE))
  local body="{\"side\":\"${SIDE_LABEL}\",\"v\":$((ok+err+1)),\"src\":\"w5\",\"@metadata\":{\"@collection\":\"Conflicted\"}}"
  if is_put_ok "$(put_doc "${ID_PREFIX}/${n}" "$body")"; then ok=$((ok+1)); else err=$((err+1)); fi
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
echo "W-5 starting  ${TARGET}/${DB_NAME}  prefix=${ID_PREFIX}  pool=${POOL_SIZE}  side=${SIDE_LABEL}  duration=${duration}s" | tee -a "$logfile" >&2
run_loop
elapsed=$(( $(date +%s) - start ))
echo "W-5 DONE  ${TARGET}/${DB_NAME}  side=${SIDE_LABEL}  ops=$((ok+err))  ok=$ok  err=$err  elapsed=${elapsed}s" | tee -a "$logfile" >&2
