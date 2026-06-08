#!/usr/bin/env bash
# W-7 reader -- pairs with workload_w7.sh.  RV-1 Phase 3 spec:
#   every SAMPLE_INTERVAL (default 30s):  GET full revision history; log elapsed_ms.
#   every STATS_INTERVAL  (default 300s): GET /stats; log SizeOnDisk + CountOfRevisionDocuments.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${DURATION_SECS:?}" "${DOC_ID:?}"
: "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"

sample_interval="${SAMPLE_INTERVAL:-30}"
stats_interval="${STATS_INTERVAL:-300}"
log_file="${LOG_FILE:-/tmp/w7-reader-${TARGET}-${DB_NAME}.log}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w7r-${TARGET}-${DB_NAME}.pid"

setup_pidfile() {
  echo $$ > "$pidfile"
  trap 'rm -f "$pidfile"' EXIT
}

# Time one full /revisions fetch.  Echoes "http=<code>  elapsed_ms=<ms>".
sample_history_fetch() {
  local start_ns http elapsed_ms
  start_ns=$(date +%s%N)
  http=$(curl --connect-timeout 5 --max-time 15 -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    "$url/revisions?id=$DOC_ID&pageSize=1000000")
  elapsed_ms=$(( ($(date +%s%N) - start_ns) / 1000000 ))
  echo "http=$http  elapsed_ms=$elapsed_ms"
}

# Echo "SizeInBytes=<n>  CountOfRevisionDocuments=<n>" from /stats.
sample_stats() {
  local stats size revs
  stats=$(curl --connect-timeout 5 --max-time 15 -sk --cert "$CERT_PEM" --cacert "$CA_CRT" "$url/stats" || echo '{}')
  size=$(echo "$stats" | grep -oE '"SizeInBytes":[0-9]+' | head -1)
  revs=$(echo "$stats" | grep -oE '"CountOfRevisionDocuments":[0-9]+' | head -1)
  echo "${size:-SizeInBytes=?}  ${revs:-CountOfRevisionDocuments=?}"
}

# ---------- main ----------

setup_pidfile
deadline=$(( $(date +%s) + DURATION_SECS ))
last_stats=0
samples=0

echo "# W-7 reader log  ${TARGET}/${DB_NAME}  doc=$DOC_ID  started=$(date -u +%FT%TZ)" >> "$log_file"
echo "W-7 reader starting  ${TARGET}/${DB_NAME}  doc=$DOC_ID  sample=${sample_interval}s  stats=${stats_interval}s  log=$log_file"

while [ "$(date +%s)" -lt "$deadline" ]; do
  echo "$(date -u +%FT%TZ)  history_fetch  $(sample_history_fetch)" >> "$log_file"
  samples=$((samples+1))

  now=$(date +%s)
  if [ $(( now - last_stats )) -ge "$stats_interval" ]; then
    echo "$(date -u +%FT%TZ)  stats_snapshot  $(sample_stats)" >> "$log_file"
    last_stats=$now
  fi

  sleep "$sample_interval"
done

echo "W-7 reader DONE  samples=$samples  log=$log_file"
