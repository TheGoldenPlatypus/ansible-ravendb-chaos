#!/usr/bin/env bash
# W-3 -- RV-1 Phase 2: 8 concurrent writers each picking a random doc from a hot pool and
# racing through: delete -> revert-from-revision -> put -> add-attachment -> remove-attachment.
# revert-from-revision = GET prior revision via /revisions, PUT its body back as the live doc.
# Requires `jq` on the controller.

set -u

: "${TARGET:?}" "${DB_NAME:?}" "${DURATION_SECS:?}" "${ID_PREFIX:?}" "${POOL_SIZE:?}"
: "${RAVEN_DOMAIN:?}" "${CERT_PEM:?}" "${CA_CRT:?}"
command -v jq >/dev/null || { echo "ERROR: jq is required (apt install jq)" >&2; exit 4; }

writers="${WRITERS:-8}"
collection="${COLLECTION:-HotDocs}"
url="https://${TARGET}.${RAVEN_DOMAIN}:443/databases/${DB_NAME}"
pidfile="/tmp/w3-${TARGET}-${DB_NAME}.pid"

# ---------- worker -- one churn iteration ----------

churn_doc() {
  local worker_num="$1" id="$2" prior

  # 1. delete
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" -X DELETE "$url/docs?id=$id" || true

  # 2. revert-from-revision: PUT the prior live revision's body back as the live doc.
  #    Results[0] is the just-created delete-revision, Results[1] is the prior live one.
  prior=$(curl --connect-timeout 5 --max-time 15 -sk --cert "$CERT_PEM" --cacert "$CA_CRT" "$url/revisions?id=$id&pageSize=2" \
            | jq -c '.Results[1] // empty' 2>/dev/null || echo "")
  if [ -n "$prior" ]; then
    curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
      -X PUT "$url/docs?id=$id" -H 'Content-Type: application/json' -d "$prior" || true
  fi

  # 3. put new live doc
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/docs?id=$id" -H 'Content-Type: application/json' \
    -d "{\"src\":\"put\",\"w\":$worker_num,\"v\":\"p-$RANDOM\",\"@metadata\":{\"@collection\":\"$collection\"}}" || true

  # 4. add attachment
  printf 'churn-w%d-%d' "$worker_num" "$RANDOM" | curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X PUT "$url/attachments?id=$id&name=churn&contentType=application/octet-stream" --data-binary @- || true

  # 5. remove attachment
  curl --connect-timeout 5 --max-time 15 -sk -o /dev/null --cert "$CERT_PEM" --cacert "$CA_CRT" \
    -X DELETE "$url/attachments?id=$id&name=churn" || true
}

run_worker() {
  local worker_num="$1" deadline="$2" ops=0 id n
  while [ "$(date +%s)" -lt "$deadline" ]; do
    n=$((RANDOM % POOL_SIZE))
    id="${ID_PREFIX}/$n"
    churn_doc "$worker_num" "$id"
    ops=$((ops+1))
  done
  echo "  W-3 worker $worker_num  iterations=$ops"
}

# ---------- supervisor ----------

worker_pids=()

setup_pidfile() {
  echo $$ > "$pidfile"
}

cleanup() {
  for p in "${worker_pids[@]}"; do kill "$p" 2>/dev/null || true; done
  rm -f "$pidfile"
  exit 0
}

fork_workers() {
  local deadline=$(( $(date +%s) + DURATION_SECS )) w
  for ((w=1; w<=writers; w++)); do
    run_worker "$w" "$deadline" &
    worker_pids+=($!)
  done
}

# Wait for every worker.  Surface silent subshell death (OOM / signal) as non-zero exit.
wait_for_workers() {
  local failed=0 pid
  for pid in "${worker_pids[@]}"; do
    wait "$pid" || failed=$((failed+1))
  done
  if [ "$failed" -gt 0 ]; then
    echo "W-3 FAILED  ${TARGET}/${DB_NAME}  $failed/$writers workers exited non-zero"
    exit 5
  fi
}

# ---------- main ----------

setup_pidfile
trap cleanup INT TERM
echo "W-3 starting  ${TARGET}/${DB_NAME}  writers=$writers  pool=${ID_PREFIX}/0..$((POOL_SIZE-1))  duration=${DURATION_SECS}s"
fork_workers
wait_for_workers
rm -f "$pidfile"
echo "W-3 DONE  ${TARGET}/${DB_NAME}  writers=$writers  duration=${DURATION_SECS}s"
