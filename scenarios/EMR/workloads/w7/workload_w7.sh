#!/usr/bin/env bash
# W-7 -- single-writer revision firehose on one doc.  RV-1 Phase 3: 16k revs/min target for
# 60 min -> 1M revisions on users/hot.  No concurrency; runs as fast as TLS allows.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${DURATION_SECS:?}" "${DOC_ID:?}"
: "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"

collection="${COLLECTION:-HotDocs}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w7-${TARGET}-${DB_NAME}.pid"

setup_pidfile() {
  echo $$ > "$pidfile"
  trap 'rm -f "$pidfile"' EXIT
}

put_rev() {
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/docs?id=$DOC_ID" -H 'Content-Type: application/json' -d "$1" || true
}

# ---------- main ----------

setup_pidfile
start=$(date +%s)
deadline=$(( start + DURATION_SECS ))
n=0
echo "W-7 starting  ${TARGET}/${DB_NAME}  doc=$DOC_ID  duration=${DURATION_SECS}s"

while [ "$(date +%s)" -lt "$deadline" ]; do
  put_rev "{\"v\":\"$n\",\"ts\":\"$(date +%s%N)\",\"@metadata\":{\"@collection\":\"$collection\"}}"
  n=$((n+1))
done

elapsed=$(( $(date +%s) - start ))
rate=$(( elapsed > 0 ? (n * 60) / elapsed : 0 ))
echo "W-7 DONE  ${TARGET}/${DB_NAME}  doc=$DOC_ID  PUTs=$n  elapsed=${elapsed}s  rate=${rate}/min  (target 16000/min for 60min -> 1M)"
