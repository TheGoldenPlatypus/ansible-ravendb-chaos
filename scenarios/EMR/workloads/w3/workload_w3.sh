#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w3/workload_w3.sh
#
# W-3 -- concurrent churn races on a "hot" doc pool.  Per the RV-1 Phase 2 spec:
#   "8-writer concurrent loop on the 1000 hot docs: each writer picks a random doc and runs
#    delete -> revert-from-revision -> put -> add attachment -> remove attachment for 5 min.
#    Each write creates a new revision on the just-migrated docs, exercising the legacy raw-CV
#    -> hashed re-keying path (R-03)."
#
# This script forks WRITERS subshells (default 8), each picking a random doc id from the pool
# and racing through the 5-op iteration until DURATION_SECS elapses.  The parent process owns
# the pidfile so `toolbox/workloads/stop_workload.yml` cleanly kills the whole tree (TERM
# propagates to subshells via the trap).  After all subshells finish, the parent checks each
# one's exit code via `wait $pid` and exits non-zero (5) if ANY worker exited non-zero --
# this catches silent subshell death (OOM kill, bash error, signal) which a bare `wait`
# would mask under a green exit code.
#
# NOTE on "revert-from-revision".  RavenDB's POST /databases/*/revisions/revert endpoint is
# time-window + collection scoped, NOT per-doc -- it would revert the whole collection to T-N
# seconds, which is wrong for the per-doc churn pattern the spec calls for.  The real per-doc
# "restore from revision" mechanic (used by toolbox/writes/restore_revision.yml) is: GET the
# prior revision via /revisions?id=X&pageSize=2 and PUT its body back as the live doc.  That
# creates a new revision row carrying the prior body -- which is exactly the storage path the
# RV-1 R-03 raw-CV -> hashed re-keying surface exercises.  Requires `jq` on the controller.
#
# Runs for $DURATION_SECS seconds then exits.  Drops a pidfile at /tmp/w3-<target>-<db>.pid.
#
# REQUIRED env vars:
#   TARGET          node name (e.g. 1a) -- writes are directed at https://<TARGET>.<RAVEN_DOMAIN>:443
#   DB_NAME         database name
#   DURATION_SECS   wall-clock seconds before the loop exits naturally
#   ID_PREFIX       hot-doc pool prefix (e.g. "hot")
#   POOL_SIZE       hot-doc pool size (RV-1 spec: 1000)
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
#
# OPTIONAL env vars:
#   WRITERS         concurrent writer count.  Default 8 per the RV-1 spec.
#   COLLECTION      RavenDB collection name for the PUTs.  Default "HotDocs".
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${DURATION_SECS:?DURATION_SECS env var is required}"
: "${ID_PREFIX:?ID_PREFIX env var is required (hot-doc pool prefix)}"
: "${POOL_SIZE:?POOL_SIZE env var is required (hot-doc pool size; RV-1 spec = 1000)}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq is required for the revert-from-revision step (apt install jq)" >&2; exit 4; }

WRITERS="${WRITERS:-8}"
COLLECTION="${COLLECTION:-HotDocs}"
URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
PIDFILE="/tmp/w3-${TARGET}-${DB_NAME}.pid"

echo "W-3 starting  target=$TARGET  db=$DB_NAME  duration=${DURATION_SECS}s  writers=$WRITERS  pool=${ID_PREFIX}/0..$((POOL_SIZE-1))  pidfile=$PIDFILE"

echo $$ > "$PIDFILE"
WORKER_PIDS=()
cleanup() {
  for p in "${WORKER_PIDS[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -f "$PIDFILE"
  exit 0
}
trap cleanup INT TERM

END=$(( $(date +%s) + DURATION_SECS ))

for ((w=1; w<=WRITERS; w++)); do
  (
    OPS=0
    while [ $(date +%s) -lt $END ]; do
      n=$((RANDOM % POOL_SIZE))
      id="${ID_PREFIX}/$n"

      # 1. DELETE
      curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
        -X DELETE "$URL/docs?id=$id" || true

      # 2. revert-from-revision -- GET the prior revision and PUT its body back as the live
      # doc.  pageSize=2 returns the just-created delete-revision (Results[0]) + the last
      # live revision (Results[1]); we PUT Results[1] back.  Skip if no prior revision exists
      # (fresh doc, lookup failed, etc.) -- the put-new step (#3) still drives the workload.
      PRIOR=$(curl -sk --cert "$CERT_PEM" --cacert "$CA_CRT" \
        "$URL/revisions?id=$id&pageSize=2" | jq -c '.Results[1] // empty' 2>/dev/null || echo "")
      if [ -n "$PRIOR" ]; then
        curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
          -X PUT "$URL/docs?id=$id" \
          -H "Content-Type: application/json" \
          -d "$PRIOR" || true
      fi

      # 3. PUT new (creates another revision)
      curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
        -X PUT "$URL/docs?id=$id" \
        -H "Content-Type: application/json" \
        -d "{\"src\":\"put\",\"w\":$w,\"v\":\"p-$RANDOM\",\"@metadata\":{\"@collection\":\"$COLLECTION\"}}" || true

      # 4. add attachment
      printf 'churn-w%d-%d' "$w" "$RANDOM" | curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
        -X PUT "$URL/attachments?id=$id&name=churn&contentType=application/octet-stream" \
        --data-binary @- || true

      # 5. remove attachment
      curl -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
        -X DELETE "$URL/attachments?id=$id&name=churn" || true

      OPS=$((OPS+1))
    done
    echo "  W-3 worker $w  iterations=$OPS"
  ) &
  WORKER_PIDS+=($!)
done

FAILED=0
FAILED_PIDS=()
for pid in "${WORKER_PIDS[@]}"; do
  if ! wait "$pid"; then
    rc=$?
    FAILED=$((FAILED+1))
    FAILED_PIDS+=("pid=$pid rc=$rc")
  fi
done

rm -f "$PIDFILE"

if [ "$FAILED" -gt 0 ]; then
  echo "W-3 FAILED  target=$TARGET  db=$DB_NAME  $FAILED/$WRITERS workers exited non-zero: ${FAILED_PIDS[*]}"
  exit 5
fi
echo "W-3 DONE  target=$TARGET  db=$DB_NAME  writers=$WRITERS  duration=${DURATION_SECS}s  all workers exited 0"
