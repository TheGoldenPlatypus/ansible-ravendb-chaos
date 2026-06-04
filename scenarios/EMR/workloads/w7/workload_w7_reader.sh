#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w7/workload_w7_reader.sh
#
# W-7 concurrent reader.  Per the RV-1 Phase 3 spec:
#   "Concurrent reader fetches full revision history every 30 s; record p99 read latency.
#    Every 5 min: capture voron stats, revision count, p99 read latency."
#
# Every $SAMPLE_INTERVAL seconds: GET /revisions?id=$DOC_ID&pageSize=1000000, time the request,
# and append a one-line record to $LOG_FILE.  Every $STATS_INTERVAL seconds: additionally
# GET /stats and dump SizeOnDisk + CountOfRevisionDocuments.
#
# Drops a pidfile at /tmp/w7r-<target>-<db>.pid for clean shutdown via stop_workload.yml.
#
# REQUIRED env vars:
#   TARGET          node name
#   DB_NAME         database name
#   DURATION_SECS   wall-clock seconds
#   DOC_ID          single doc whose revision history we read
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
#
# OPTIONAL env vars:
#   SAMPLE_INTERVAL   seconds between history-fetch samples.  Default 30 (RV-1 spec).
#   STATS_INTERVAL    seconds between /stats snapshots.  Default 300 (RV-1 spec: every 5 min).
#   LOG_FILE          path to append the per-sample records.
#                     Default /tmp/w7-reader-<target>-<db>.log
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${DURATION_SECS:?DURATION_SECS env var is required}"
: "${DOC_ID:?DOC_ID env var is required}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-30}"
STATS_INTERVAL="${STATS_INTERVAL:-300}"
LOG_FILE="${LOG_FILE:-/tmp/w7-reader-${TARGET}-${DB_NAME}.log}"
URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
PIDFILE="/tmp/w7r-${TARGET}-${DB_NAME}.pid"

echo "W-7 reader starting  target=$TARGET  db=$DB_NAME  doc=$DOC_ID  sample=${SAMPLE_INTERVAL}s  stats=${STATS_INTERVAL}s  log=$LOG_FILE  pidfile=$PIDFILE"

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

END=$(( $(date +%s) + DURATION_SECS ))
LAST_STATS=0
SAMPLES=0

# header
echo "# W-7 reader log  target=$TARGET  db=$DB_NAME  doc=$DOC_ID  started=$(date -u +%FT%TZ)" >> "$LOG_FILE"

while [ $(date +%s) -lt $END ]; do
  # full revision-history fetch (latency probe)
  START_NS=$(date +%s%N)
  HTTP=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
    "$URL/revisions?id=$DOC_ID&pageSize=1000000")
  END_NS=$(date +%s%N)
  ELAPSED_MS=$(( (END_NS - START_NS) / 1000000 ))
  echo "$(date -u +%FT%TZ)  history_fetch  http=$HTTP  elapsed_ms=$ELAPSED_MS" >> "$LOG_FILE"
  SAMPLES=$((SAMPLES+1))

  # periodic /stats snapshot (size + revision count)
  NOW=$(date +%s)
  if [ $((NOW - LAST_STATS)) -ge $STATS_INTERVAL ]; then
    STATS=$(curl -sk --cert "$CERT_PEM" --cacert "$CA_CRT" "$URL/stats" || echo '{}')
    SIZE=$(echo "$STATS" | grep -oE '"SizeInBytes":[0-9]+' | head -1)
    REVS=$(echo "$STATS" | grep -oE '"CountOfRevisionDocuments":[0-9]+' | head -1)
    echo "$(date -u +%FT%TZ)  stats_snapshot  ${SIZE:-SizeInBytes=?}  ${REVS:-CountOfRevisionDocuments=?}" >> "$LOG_FILE"
    LAST_STATS=$NOW
  fi

  sleep "$SAMPLE_INTERVAL"
done

rm -f "$PIDFILE"
echo "W-7 reader DONE  samples=$SAMPLES  log=$LOG_FILE"
