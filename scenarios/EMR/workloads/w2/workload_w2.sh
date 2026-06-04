#!/usr/bin/env bash
##################################################################################################
# scenarios/EMR/workloads/w2/workload_w2.sh
#
# W-2 -- doc-extension churn.  Per EMR plan:
#   25% attachment-add  / 25% attachment-remove
#   12.5% counter-inc   / 12.5% counter-dec
#   12.5% TS-append     / 12.5% TS-delete
# NO doc CRUD (that's W-1's job).  Targets docs already in the seeded pool.
#
# TWO MODES (same as W-1):
#   * Single-bucket (legacy): set ID_PREFIX + POOL_SIZE.
#   * Multi-bucket (RPV-1):  set BUCKETS_SPEC -- pipe-separated "prefix:pool_size:weight"
#     triples.  Each op picks a bucket weighted-random.
#
# Runs for $DURATION_SECS seconds then exits.  Drops a pidfile at /tmp/w2-<target>-<db>.pid for
# the parent scenario to kill cleanly via toolbox/workloads/stop_workload.yml.
#
# REQUIRED env vars:
#   TARGET          node name (e.g. 1a)
#   DB_NAME         database name
#   DURATION_SECS   wall-clock seconds before the loop exits naturally
#   RAVEN_DOMAIN    e.g. hubsink.test
#   CERT_PEM        client cert pem path
#   CA_CRT          ca cert path
#
# EXACTLY ONE OF:
#   ID_PREFIX + POOL_SIZE         single-bucket mode (legacy)
#   BUCKETS_SPEC                  multi-bucket mode (see W-1 for format)
##################################################################################################

set -u

: "${TARGET:?TARGET env var is required}"
: "${DB_NAME:?DB_NAME env var is required}"
: "${RAVEN_DOMAIN:?RAVEN_DOMAIN env var is required}"
: "${CERT_PEM:?CERT_PEM env var is required}"
: "${CA_CRT:?CA_CRT env var is required}"

# DURATION_SECS is OPTIONAL.  Unset or 0 = run indefinitely (until SIGTERM / SIGKILL from
# `toolbox/workloads/stop_workload.yml`).  Per Karmel's plan, RPV-1/RV-1 workloads run
# "continuous from T0 through endpoint" -- the scenario is responsible for killing them
# explicitly via stop_workload.  Setting a positive DURATION_SECS makes the script self-exit.
DURATION_SECS="${DURATION_SECS:-0}"

URL="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
PIDFILE="/tmp/w2-${TARGET}-${DB_NAME}.pid"

PREFIXES=()
POOLS=()
CUM_WEIGHTS=()
TOTAL_WEIGHT=0

if [ -n "${BUCKETS_SPEC:-}" ]; then
  IFS='|' read -ra BUCKETS_ARR <<< "$BUCKETS_SPEC"
  for entry in "${BUCKETS_ARR[@]}"; do
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
echo "W-2 starting  target=$TARGET  db=$DB_NAME  duration=${DURATION_SECS}s  buckets=$NUM_BUCKETS  total_weight=$TOTAL_WEIGHT"
for ((i=0; i<NUM_BUCKETS; i++)); do
  echo "  bucket $i: prefix=${PREFIXES[$i]}  pool_size=${POOLS[$i]}  cum_weight=${CUM_WEIGHTS[$i]}"
done

echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"; exit 0' INT TERM

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
  CUR_PREFIX="${PREFIXES[$((NUM_BUCKETS-1))]}"
  CUR_POOL="${POOLS[$((NUM_BUCKETS-1))]}"
}

# DURATION_SECS=0 -> END=0 -> "no deadline" sentinel; loop runs until killed.
END=0
[ "$DURATION_SECS" -gt 0 ] && END=$(( $(date +%s) + DURATION_SECS ))
ATT_ADD=0; ATT_RM=0; CTR_INC=0; CTR_DEC=0; TS_APP=0; TS_DEL=0; ERRORS=0

while true; do
  [ "$END" -gt 0 ] && [ $(date +%s) -ge $END ] && break
  pick_bucket
  r=$((RANDOM % 100))
  n=$((RANDOM % CUR_POOL))
  id="${CUR_PREFIX}/$n"

  if [ $r -lt 25 ]; then
    # attachment-add
    aname="att-w2-$RANDOM"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$URL/attachments?id=$(printf %s "$id" | sed 's|/|%2F|g')&name=$aname" \
      -H "Content-Type: application/octet-stream" \
      --data-raw "w2-payload-$RANDOM")
    if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then ATT_ADD=$((ATT_ADD+1)); else ERRORS=$((ERRORS+1)); fi

  elif [ $r -lt 50 ]; then
    # attachment-remove (best-effort -- 404 tolerated)
    aname="att-w2-$((RANDOM % 1000))"
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X DELETE "$URL/attachments?id=$(printf %s "$id" | sed 's|/|%2F|g')&name=$aname")
    if [ "$code" = "204" ] || [ "$code" = "404" ]; then ATT_RM=$((ATT_RM+1)); else ERRORS=$((ERRORS+1)); fi

  elif [ $r -lt 62 ]; then
    # counter-inc
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X POST "$URL/counters" \
      -H "Content-Type: application/json" \
      --data-raw "{\"Documents\":[{\"DocumentId\":\"$id\",\"Operations\":[{\"Type\":\"Increment\",\"CounterName\":\"Likes\",\"Delta\":1}]}]}")
    if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then CTR_INC=$((CTR_INC+1)); else ERRORS=$((ERRORS+1)); fi

  elif [ $r -lt 75 ]; then
    # counter-dec
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X POST "$URL/counters" \
      -H "Content-Type: application/json" \
      --data-raw "{\"Documents\":[{\"DocumentId\":\"$id\",\"Operations\":[{\"Type\":\"Increment\",\"CounterName\":\"Likes\",\"Delta\":-1}]}]}")
    if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then CTR_DEC=$((CTR_DEC+1)); else ERRORS=$((ERRORS+1)); fi

  elif [ $r -lt 87 ]; then
    # TS-append
    ts=$(date -u -d "$((RANDOM % 3600)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X POST "$URL/timeseries?docId=$(printf %s "$id" | sed 's|/|%2F|g')" \
      -H "Content-Type: application/json" \
      --data-raw "{\"Name\":\"Heartrate\",\"Appends\":[{\"Timestamp\":\"$ts\",\"Values\":[$((RANDOM % 100))],\"Tag\":null}]}")
    if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then TS_APP=$((TS_APP+1)); else ERRORS=$((ERRORS+1)); fi

  else
    # TS-delete-range (best-effort -- empty range tolerated)
    from=$(date -u -d "$((1800 + RANDOM % 3600)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
    to=$(date -u -d "$((900 + RANDOM % 1800)) seconds ago" +%Y-%m-%dT%H:%M:%S.000Z)
    code=$(curl -sk -o /dev/null -w '%{http_code}' --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X POST "$URL/timeseries?docId=$(printf %s "$id" | sed 's|/|%2F|g')" \
      -H "Content-Type: application/json" \
      --data-raw "{\"Name\":\"Heartrate\",\"Deletes\":[{\"From\":\"$from\",\"To\":\"$to\"}]}")
    if [ "$code" = "200" ] || [ "$code" = "201" ] || [ "$code" = "204" ]; then TS_DEL=$((TS_DEL+1)); else ERRORS=$((ERRORS+1)); fi
  fi
  sleep 2.4   # ~25 ops/min per the RPV-1 spec ("~50 ops/min total", W-1 + W-2 combined)
done

rm -f "$PIDFILE"
echo "W-2 DONE  target=$TARGET  db=$DB_NAME  att_add=$ATT_ADD  att_rm=$ATT_RM  ctr_inc=$CTR_INC  ctr_dec=$CTR_DEC  ts_app=$TS_APP  ts_del=$TS_DEL  errors=$ERRORS"
