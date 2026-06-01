#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w1/workload_w1.sh
#
# W-1 -- continuous doc CRUD mix (70% update / 20% put-new / 10% delete) on a seeded doc pool.
# Runs for $DURATION_SECS seconds then exits.  Drops a pidfile at /tmp/w1-<target>-<db>.pid so a
# parent scenario can kill it cleanly via toolbox/workloads/stop_workload.yml before duration
# elapses.
#
# Driven by workload_w1.yml (ansible wrapper) but standalone-runnable for debugging.
#
# REQUIRED env vars:
#   TARGET          node name (e.g. 1a) -- writes are directed at https://<TARGET>.<RAVEN_DOMAIN>:443
#   DB_NAME         database name
#   ID_PREFIX       prefix of the seeded pool ids (e.g. "seed" -> "seed/0", "seed/1", ...)
#   POOL_SIZE       number of distinct doc ids in the pool
#   DURATION_SECS   wall-clock seconds before the loop exits naturally
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${ID_PREFIX:?ID_PREFIX env var is required}"
: "${POOL_SIZE:?POOL_SIZE env var is required}"
: "${DURATION_SECS:?DURATION_SECS env var is required}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
PIDFILE="/tmp/w1-${TARGET}-${DB_NAME}.pid"

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

END=$(( $(date +%s) + DURATION_SECS ))
UPDATES=0; PUTS=0; DELETES=0; ERRORS=0

while [ $(date +%s) -lt $END ]; do
  r=$((RANDOM % 100))
  if [ $r -lt 70 ]; then
    # update on a pool id
    n=$((RANDOM % POOL_SIZE))
    id="${ID_PREFIX}/$n"
    body="{\"v\":\"u-$RANDOM\",\"ts\":\"$(date +%s%N)\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$URL/docs?id=$id" \
      -H "Content-Type: application/json" -d "$body")
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then UPDATES=$((UPDATES+1)); else ERRORS=$((ERRORS+1)); fi
  elif [ $r -lt 90 ]; then
    # put new id outside the seeded pool
    id="${ID_PREFIX}/new-$$-$RANDOM"
    body="{\"v\":\"new\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$URL/docs?id=$id" \
      -H "Content-Type: application/json" -d "$body")
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then PUTS=$((PUTS+1)); else ERRORS=$((ERRORS+1)); fi
  else
    # delete a pool id
    n=$((RANDOM % POOL_SIZE))
    id="${ID_PREFIX}/$n"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X DELETE "$URL/docs?id=$id")
    if [ "$code" = "204" ] || [ "$code" = "404" ]; then DELETES=$((DELETES+1)); else ERRORS=$((ERRORS+1)); fi
  fi
  sleep 0.1   # ~10 ops/sec -- gives replication brief windows to settle for quiescence checks
done

rm -f "$PIDFILE"
echo "W-1 DONE  target=$TARGET  db=$DB_NAME  updates=$UPDATES  puts=$PUTS  deletes=$DELETES  errors=$ERRORS"
