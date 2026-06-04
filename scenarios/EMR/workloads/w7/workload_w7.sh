#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w7/workload_w7.sh
#
# W-7 -- single-writer revision firehose on one doc.  Per the RV-1 Phase 3 spec:
#   "Single-writer W-7 on users/hot: 16k revs/min target for 60 min -> 1M revisions."
#
# One curl PUT loop, no concurrency.  The 16k revs/min figure is the spec's TARGET, not a hard
# requirement -- the loop runs as fast as a single writer + TLS handshake can sustain; the
# final count is whatever it achieves in DURATION_SECS, printed at exit so the operator can
# compare against the 1M target.  This is the literal "single writer" the spec calls for.
#
# Runs for $DURATION_SECS seconds then exits.  Drops a pidfile at /tmp/w7-<target>-<db>.pid
# so the parent scenario can kill it cleanly via toolbox/workloads/stop_workload.yml.
#
# REQUIRED env vars:
#   TARGET          node name (e.g. 1a)
#   DB_NAME         database name
#   DURATION_SECS   wall-clock seconds before the loop exits naturally (RV-1 spec: 3600)
#   DOC_ID          single doc to hammer (RV-1 spec: users/hot)
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
#
# OPTIONAL env vars:
#   COLLECTION      RavenDB collection name.  Default "HotDocs".
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${DURATION_SECS:?DURATION_SECS env var is required}"
: "${DOC_ID:?DOC_ID env var is required (RV-1 spec: users/hot)}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

COLLECTION="${COLLECTION:-HotDocs}"
URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
PIDFILE="/tmp/w7-${TARGET}-${DB_NAME}.pid"

echo "W-7 starting  target=$TARGET  db=$DB_NAME  doc=$DOC_ID  duration=${DURATION_SECS}s  pidfile=$PIDFILE"

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

END=$(( $(date +%s) + DURATION_SECS ))
START_TS=$(date +%s)
C=0

while [ $(date +%s) -lt $END ]; do
  curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$URL/docs?id=$DOC_ID" \
    -H "Content-Type: application/json" \
    -d "{\"v\":\"$C\",\"ts\":\"$(date +%s%N)\",\"@metadata\":{\"@collection\":\"$COLLECTION\"}}" || true
  C=$((C+1))
done

ELAPSED=$(( $(date +%s) - START_TS ))
RATE_PER_MIN=$(( ELAPSED > 0 ? (C * 60) / ELAPSED : 0 ))
rm -f "$PIDFILE"
echo "W-7 DONE  target=$TARGET  db=$DB_NAME  doc=$DOC_ID  PUTs=$C  elapsed=${ELAPSED}s  rate=${RATE_PER_MIN}/min  (spec target: 16000/min for 60min -> 1M)"
