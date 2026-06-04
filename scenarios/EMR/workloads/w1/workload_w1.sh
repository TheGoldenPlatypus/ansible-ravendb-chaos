#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w1/workload_w1.sh
#
# W-1 -- continuous doc CRUD mix (70% update / 20% put-new / 10% delete).
#
# TWO MODES:
#   * Single-bucket (legacy): set ID_PREFIX + POOL_SIZE.  Hits one bucket the whole run.
#   * Multi-bucket (RPV-1):  set BUCKETS_SPEC -- pipe-separated triples
#     "prefix:pool_size:weight" describing 1..N buckets.  Each op picks a bucket weighted-random.
#     Per the RPV-1 spec the per-group weighting is 40/40/20 (sink-1 / sink-2 / hub-only).
#
# Runs for $DURATION_SECS seconds then exits.  Drops a pidfile at /tmp/w1-<target>-<db>.pid so a
# parent scenario can kill it cleanly via toolbox/workloads/stop_workload.yml before duration
# elapses.
#
# Driven by workload_w1.yml (ansible wrapper) but standalone-runnable for debugging.
#
# REQUIRED env vars:
#   TARGET          node name (e.g. 1a) -- writes are directed at https://<TARGET>.<RAVEN_DOMAIN>:443
#   DB_NAME         database name
#   DURATION_SECS   wall-clock seconds before the loop exits naturally
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
#
# EXACTLY ONE OF:
#   ID_PREFIX + POOL_SIZE         single-bucket mode (legacy)
#   BUCKETS_SPEC                  multi-bucket mode, e.g.
#                                   "users/sink1:2000:20|orders/sink1:2000:20|users/sink2:2000:20|
#                                    orders/sink2:2000:20|users/hub:2000:7|orders/hub:2000:7|Internal:3000:6"
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

# DURATION_SECS is OPTIONAL.  Unset or 0 = run indefinitely (until SIGTERM / SIGKILL from
# `toolbox/workloads/stop_workload.yml`).  Per the spec, RPV-1/RV-1 workloads run
# "continuous from T0 through endpoint" -- the scenario is responsible for killing them
# explicitly via stop_workload.  Setting a positive DURATION_SECS makes the script self-exit
# after that many seconds (useful for ad-hoc invocations only).
DURATION_SECS="${DURATION_SECS:-0}"

URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
# Optional WRITER_ID suffix lets multiple W-1 instances run in parallel against the same
# (target, db) without colliding on the pidfile (RV-1 Phase 1 launches W-1 x 4).  Unset =
# legacy single-writer behavior (no suffix) preserved for RPV-1's contract.
PIDFILE="/tmp/w1-${TARGET}-${DB_NAME}${WRITER_ID:+-w${WRITER_ID}}.pid"

# --- bucket parsing -------------------------------------------------------------------------
# Resolve either mode into two parallel arrays (prefixes, pools) and a parallel cumulative-weight
# array (cum_weights).  Per-iteration we pick an index i where cum_weights[i] > rnd_mod_total.
PREFIXES=()
POOLS=()
CUM_WEIGHTS=()
TOTAL_WEIGHT=0

if [ -n "${BUCKETS_SPEC:-}" ]; then
  IFS='|' read -ra BUCKETS_ARR <<< "$BUCKETS_SPEC"
  for entry in "${BUCKETS_ARR[@]}"; do
    # split on ':' into 3 fields
    IFS=':' read -r p pool w <<< "$entry"
    if [ -z "$p" ] || [ -z "$pool" ] || [ -z "$w" ]; then
      echo "ERROR: malformed BUCKETS_SPEC entry '$entry' (expected prefix:pool_size:weight)" >&2
      exit 2
    fi
    PREFIXES+=("$p")
    POOLS+=("$pool")
    TOTAL_WEIGHT=$(( TOTAL_WEIGHT + w ))
    CUM_WEIGHTS+=("$TOTAL_WEIGHT")
  done
elif [ -n "${ID_PREFIX:-}" ] && [ -n "${POOL_SIZE:-}" ]; then
  PREFIXES+=("$ID_PREFIX")
  POOLS+=("$POOL_SIZE")
  TOTAL_WEIGHT=1
  CUM_WEIGHTS+=("1")
else
  echo "ERROR: set either (ID_PREFIX + POOL_SIZE) or BUCKETS_SPEC" >&2
  exit 2
fi

NUM_BUCKETS=${#PREFIXES[@]}
echo "W-1 starting  target=$TARGET  db=$DB_NAME  duration=${DURATION_SECS}s  buckets=$NUM_BUCKETS  total_weight=$TOTAL_WEIGHT${WRITER_ID:+  writer=$WRITER_ID}  pidfile=$PIDFILE"
for ((i=0; i<NUM_BUCKETS; i++)); do
  echo "  bucket $i: prefix=${PREFIXES[$i]}  pool_size=${POOLS[$i]}  cum_weight=${CUM_WEIGHTS[$i]}"
done

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

# Pick a weighted-random bucket index and set CUR_PREFIX, CUR_POOL.
pick_bucket() {
  local r=$((RANDOM % TOTAL_WEIGHT))
  local i
  for ((i=0; i<NUM_BUCKETS; i++)); do
    if [ $r -lt ${CUM_WEIGHTS[$i]} ]; then
      CUR_PREFIX="${PREFIXES[$i]}"
      CUR_POOL="${POOLS[$i]}"
      return
    fi
  done
  # fallback to last bucket (shouldn't happen)
  CUR_PREFIX="${PREFIXES[$((NUM_BUCKETS-1))]}"
  CUR_POOL="${POOLS[$((NUM_BUCKETS-1))]}"
}

# DURATION_SECS=0 -> END=0 -> "no deadline" sentinel; loop runs until killed.
END=0
[ "$DURATION_SECS" -gt 0 ] && END=$(( $(date +%s) + DURATION_SECS ))
UPDATES=0; PUTS=0; DELETES=0; ERRORS=0

while true; do
  [ "$END" -gt 0 ] && [ $(date +%s) -ge $END ] && break
  pick_bucket
  r=$((RANDOM % 100))
  if [ $r -lt 70 ]; then
    # update on a pool id
    n=$((RANDOM % CUR_POOL))
    id="${CUR_PREFIX}/$n"
    body="{\"v\":\"u-$RANDOM\",\"ts\":\"$(date +%s%N)\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$URL/docs?id=$id" \
      -H "Content-Type: application/json" -d "$body")
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then UPDATES=$((UPDATES+1)); else ERRORS=$((ERRORS+1)); fi
  elif [ $r -lt 90 ]; then
    # put new id outside the seeded pool (still under the bucket's prefix)
    id="${CUR_PREFIX}/new-$$-$RANDOM"
    body="{\"v\":\"new\",\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$URL/docs?id=$id" \
      -H "Content-Type: application/json" -d "$body")
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then PUTS=$((PUTS+1)); else ERRORS=$((ERRORS+1)); fi
  else
    # delete a pool id
    n=$((RANDOM % CUR_POOL))
    id="${CUR_PREFIX}/$n"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X DELETE "$URL/docs?id=$id")
    if [ "$code" = "204" ] || [ "$code" = "404" ]; then DELETES=$((DELETES+1)); else ERRORS=$((ERRORS+1)); fi
  fi
  sleep 2.4   # ~25 ops/min per the RPV-1 spec ("~50 ops/min total", W-1 + W-2 combined)
done

rm -f "$PIDFILE"
echo "W-1 DONE  target=$TARGET  db=$DB_NAME  updates=$UPDATES  puts=$PUTS  deletes=$DELETES  errors=$ERRORS"
