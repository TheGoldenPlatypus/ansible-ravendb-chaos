#!/usr/bin/env bash
# scripts/run_all_overnight.sh -- launch chaos scenarios overnight.
#
# Two execution shapes:
#   BATCHED=1 (default)  -- group scenarios by memory footprint and run each
#                           group in parallel, with a barrier between groups.
#                           Keeps peak container count well under kaiju's
#                           memory ceiling.  After the last group finishes,
#                           loop back to the first if LOOP=1 + deadline OK.
#   BATCHED=0            -- all 7 scenarios in parallel at once (peak ~167GB).
#                           Faster on a beefy host; risks memory pressure.
#
# Per-iteration wall-clock cap via TIMEOUT_PER_SCENARIO (default 120m).
# Each iteration produces a log file tagged .PASS / .FAIL-rcN / .TIMEOUT.
#
# USAGE:
#   export ANSIBLE_BECOME_PASS=...
#   ./scripts/run_all_overnight.sh                                # 1 batched pass
#   LOOP=1 MAX_WALL_HRS=8 ./scripts/run_all_overnight.sh          # loop until 8h elapsed
#
# OPTIONAL ENV:
#   V_OLD                   v_old version string                        (6.2.16)
#   V_NEW                   v_new .deb absolute path                    (auto)
#   V_OLD_DEB               cached v_old .deb (skip S3 download)        (auto)
#   OUT_DIR                 base log dir                                (logs/overnight)
#   TIMEOUT_PER_SCENARIO    per-iteration wall-clock cap                (120m)
#   BATCHED                 1 = staged batches / 0 = all-parallel       (1)
#   LOOP                    0 = single pass / 1 = loop until deadline   (0)
#   MAX_WALL_HRS            loop wall-clock cap                         (8)
#   MAX_ITERS_PER_SCENARIO  per-scenario iter cap (0=unlimited)         (0)
#
# OUTPUTS (under $OUT_DIR/<runts>/):
#   <scenario>-iter<N>.PASS.log            iteration succeeded
#   <scenario>-iter<N>.FAIL-rcN.log        iteration failed (non-zero rc)
#   <scenario>-iter<N>.TIMEOUT.log         iteration hit the per-scenario cap
#   <scenario>-iter<N>.running.log         still in flight (mid-run)
#   summary.txt                            one line per finished iteration
#
# MIDNIGHT CHECK:
#   cat logs/overnight/<runts>/summary.txt
#   ls  logs/overnight/<runts>/*.running.log
#   tail -f logs/overnight/<runts>/rv1-iter1.running.log

set -u

cd "$(dirname "$0")/.."

# -------------------------------------------------------------------------
# Required env
# -------------------------------------------------------------------------
: "${ANSIBLE_BECOME_PASS:?Set ANSIBLE_BECOME_PASS before running}"

# -------------------------------------------------------------------------
# Defaults
# -------------------------------------------------------------------------
V_OLD="${V_OLD:-6.2.16}"
V_NEW="${V_NEW:-$PWD/builds/ravendb_7.2.3-custom-72-0_ubuntu.24.04_amd64.deb}"
export V_OLD_DEB="${V_OLD_DEB:-$PWD/builds/ravendb_6.2.16-0_ubuntu.24.04_amd64.deb}"

OUT_DIR_BASE="${OUT_DIR:-logs/overnight}"
TIMEOUT_PER_SCENARIO="${TIMEOUT_PER_SCENARIO:-120m}"
BATCHED="${BATCHED:-1}"
LOOP="${LOOP:-0}"
MAX_WALL_HRS="${MAX_WALL_HRS:-8}"
MAX_ITERS_PER_SCENARIO="${MAX_ITERS_PER_SCENARIO:-0}"

# -------------------------------------------------------------------------
# Pre-flight
# -------------------------------------------------------------------------
[ -f "$V_NEW" ]     || { echo "ERROR: V_NEW not found: $V_NEW"; exit 2; }
[ -f "$V_OLD_DEB" ] || { echo "ERROR: V_OLD_DEB not found: $V_OLD_DEB"; exit 2; }
docker info >/dev/null 2>&1 || { echo "ERROR: docker daemon not reachable"; exit 3; }
command -v timeout >/dev/null || { echo "ERROR: 'timeout' (coreutils) not on PATH"; exit 4; }

if grep -q 'ANSIBLE MANAGED CHAOS LAB' /etc/hosts 2>/dev/null; then
  echo "Stripping stale ANSIBLE MANAGED CHAOS LAB blocks from /etc/hosts..."
  sudo sed -i '/# BEGIN ANSIBLE MANAGED CHAOS LAB/,/# END ANSIBLE MANAGED CHAOS LAB/d' /etc/hosts
fi

# -------------------------------------------------------------------------
# Dedicated per-run folder so each overnight session is self-contained.
# Logs, summary, everything go under $OUT_DIR.
# -------------------------------------------------------------------------
RUNTS="$(date +%Y%m%d-%H%M)"
OUT_DIR="$OUT_DIR_BASE/$RUNTS"
mkdir -p "$OUT_DIR"
SUMMARY="$OUT_DIR/summary.txt"

# Wall-clock deadline (consulted in LOOP=1 mode)
DEADLINE=$(( $(date +%s) + (MAX_WALL_HRS * 3600) ))

{
  echo "==== overnight run started at $(date) ===="
  echo "RUNTS:                   $RUNTS"
  echo "OUT_DIR:                 $OUT_DIR"
  echo "BATCHED:                 $BATCHED"
  echo "LOOP:                    $LOOP"
  echo "MAX_WALL_HRS:            $MAX_WALL_HRS"
  echo "MAX_ITERS_PER_SCENARIO:  $MAX_ITERS_PER_SCENARIO  (0 = unlimited)"
  echo "TIMEOUT_PER_SCENARIO:    $TIMEOUT_PER_SCENARIO"
  echo "V_OLD:                   $V_OLD"
  echo "V_NEW:                   $V_NEW"
  echo
} > "$SUMMARY"

# -------------------------------------------------------------------------
# Per-scenario iteration counters (associative array).  Each scenario tracks
# its own iter# independently, so even in batched mode every scenario gets
# 1, 2, 3, ... rather than restarting at 1 each batch.
# -------------------------------------------------------------------------
declare -A ITER_COUNTS=(
  [rv1]=0 [rp1]=0 [rpv1-A]=0 [rpv1-B]=0 [rpv1-C]=0 [rv2]=0
)

# Track scenarios that have hit MAX_ITERS_PER_SCENARIO so we skip them on
# subsequent loop passes.
declare -A DONE_SCENARIOS=()

# -------------------------------------------------------------------------
# Per-iteration runner -- runs ONE iteration with a wall-clock timeout,
# captures rc, renames log, appends to summary.  After the run completes,
# `docker stop`s every container attached to `net` so the containers stay
# around (preserved state, devs can `docker start` them in the morning to
# inspect Studio) without burning CPU all night.  Subsequent iters of the
# same scenario use a DIFFERENT network + CID (computed by the caller) so
# names never collide with stopped earlier-iter containers.
#
# Args: <scenario-name> <iter-number> <network-name> <cmd...>
# -------------------------------------------------------------------------
run_iter() {
  local name="$1"; shift
  local iter="$1"; shift
  local net="$1"; shift
  local logfile="$OUT_DIR/${name}-iter${iter}.running.log"

  {
    echo "===================================================================="
    echo "  SCENARIO: $name   iter=$iter   net=$net"
    echo "  STARTED:  $(date)"
    echo "  TIMEOUT:  $TIMEOUT_PER_SCENARIO"
    echo "  CMD:      $*"
    echo "===================================================================="
  } >> "$logfile"

  timeout --foreground "$TIMEOUT_PER_SCENARIO" "$@" >> "$logfile" 2>&1
  local rc=$?

  # Stop (don't remove) every container still attached to this iter's
  # network.  RavenDB gets a clean SIGTERM, flushes Voron, shuts down.
  # The container + data dir + logs survive for morning inspection.
  local stop_names
  stop_names=$(docker network inspect "$net" \
                  --format '{{range .Containers}}{{.Name}} {{end}}' \
                  2>/dev/null | tr -s ' ' '\n' | grep -v '^$' || true)
  if [ -n "$stop_names" ]; then
    {
      echo "--------------------------------------------------------------------"
      echo "  Stopping (NOT removing) containers for post-mortem on net=$net:"
      echo "$stop_names" | sed 's/^/    /'
      echo "--------------------------------------------------------------------"
    } >> "$logfile"
    # shellcheck disable=SC2086
    docker stop $stop_names >> "$logfile" 2>&1 || true
  fi

  {
    echo "===================================================================="
    echo "  SCENARIO: $name   iter=$iter"
    echo "  ENDED:    $(date)"
    echo "  EXIT_RC:  $rc"
    echo "  NET (kept for inspection): $net"
    echo "===================================================================="
  } >> "$logfile"

  local suffix
  case "$rc" in
    0)   suffix="PASS" ;;
    124) suffix="TIMEOUT" ;;
    *)   suffix="FAIL-rc${rc}" ;;
  esac
  local final="$OUT_DIR/${name}-iter${iter}.${suffix}.log"
  mv "$logfile" "$final"

  printf '%-12s  iter=%-3s  %-12s  net=%-22s  %s\n' \
    "$name" "$iter" "$suffix" "$net" "$(basename "$final")" >> "$SUMMARY"
}

# -------------------------------------------------------------------------
# Launch ONE iteration of each scenario in a batch in parallel, wait for
# the batch to fully drain (barrier), then return.  Skips scenarios that
# have already hit MAX_ITERS_PER_SCENARIO.
#
# Args: <scenario-names...>   (space-separated)
# -------------------------------------------------------------------------
run_batch() {
  local pids=()
  local name iter
  for name in "$@"; do
    if [ -n "${DONE_SCENARIOS[$name]:-}" ]; then
      continue
    fi
    iter=$(( ${ITER_COUNTS[$name]} + 1 ))
    ITER_COUNTS[$name]=$iter

    # Per-iter CID + NET so iters of the same scenario never collide on
    # container names (Docker requires globally-unique names per host).
    # Step is 100 -- generous spacing keeps each iter's CID range well
    # clear of the next iter's bucket.  Each iter also gets its own
    # docker network so form_clusters.yml's per-network /etc/hosts marker
    # isolates the host block.
    local cid_bump=$(( (iter - 1) * 100 ))
    local net cid cid2

    # Dispatch the per-scenario command in a background subshell.
    case "$name" in
      rv1)
        cid=$(( 1 + cid_bump ))
        net="net_rv1_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RV1/run.sh "$V_OLD" "$V_NEW" "$cid" "$net" & ;;
      rp1)
        cid=$(( 10 + cid_bump ))
        net="net_rp1_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RP1/run.sh "$V_NEW" "$cid" "$net" & ;;
      rpv1-A)
        cid=$(( 20 + cid_bump ))
        net="net_rpv1_a_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RPV1/run.sh "$V_OLD" "$V_NEW" "$cid" "$net" & ;;
      rpv1-B)
        cid=$(( 30 + cid_bump ))
        cid2=$(( cid + 1 ))
        local cid3=$(( cid + 2 ))
        net="net_rpv1_b_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RPV1/run.sh "$V_OLD" "$V_NEW" "$cid" "$net" \
          -e "{\"upgrade_step_1\":[\"${cid}a\",\"${cid}b\",\"${cid}c\"],\"upgrade_step_2\":[\"${cid2}a\",\"${cid2}b\",\"${cid2}c\"],\"upgrade_step_3\":[\"${cid3}a\",\"${cid3}b\",\"${cid3}c\"]}" & ;;
      rpv1-C)
        cid=$(( 40 + cid_bump ))
        cid2=$(( cid + 1 ))
        local cid3c=$(( cid + 2 ))
        net="net_rpv1_c_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RPV1/run.sh "$V_OLD" "$V_NEW" "$cid" "$net" \
          -e "{\"upgrade_step_1\":[\"${cid2}a\",\"${cid}b\",\"${cid3c}c\"],\"upgrade_step_2\":[\"${cid}a\",\"${cid3c}b\",\"${cid2}c\"],\"upgrade_step_3\":[\"${cid3c}a\",\"${cid}c\",\"${cid2}b\"]}" & ;;
      rv2)
        cid=$(( 50 + cid_bump ))
        net="net_rv2_iter${iter}"
        run_iter "$name" "$iter" "$net" \
          ./scenarios/company-1/RV2/run.sh "$V_OLD" "$V_NEW" "$cid" "$net" & ;;
      *)      echo "ERROR: unknown scenario '$name'" >&2; continue ;;
    esac
    pids+=($!)

    # If this iteration brings us to the per-scenario cap, mark done.
    if [ "$MAX_ITERS_PER_SCENARIO" -gt 0 ] && [ "$iter" -ge "$MAX_ITERS_PER_SCENARIO" ]; then
      DONE_SCENARIOS[$name]=1
    fi
  done

  # Barrier: wait for every backgrounded run_iter in this batch to finish.
  if [ ${#pids[@]} -gt 0 ]; then
    wait "${pids[@]}"
  fi
}

# -------------------------------------------------------------------------
# Batch layout -- chosen to keep peak container count under kaiju's memory
# ceiling.  Heaviest scenarios paired with lighter ones.
#
#   Batch A: rv1 (3) + rp1 (6) + rv2 (4)              = 13 nodes  ~33 GB
#   Batch B: rpv1-A (9) + rpv1-B (9)                  = 18 nodes  ~45 GB
#   Batch C: rpv1-C (9)                               =  9 nodes  ~23 GB
#
# Override by setting BATCH_A/B/C env vars to space-separated scenario lists.
# -------------------------------------------------------------------------
BATCH_A="${BATCH_A:-rv1 rp1 rv2}"
BATCH_B="${BATCH_B:-rpv1-A rpv1-B}"
BATCH_C="${BATCH_C:-rpv1-C}"

# -------------------------------------------------------------------------
# Main loop
# -------------------------------------------------------------------------
all_done() {
  # Returns 0 (true) when every scenario has been marked done.
  local n
  for n in rv1 rp1 rpv1-A rpv1-B rpv1-C rv2; do
    [ -n "${DONE_SCENARIOS[$n]:-}" ] || return 1
  done
  return 0
}

while :; do
  if [ "$BATCHED" = "1" ]; then
    echo "[$(date +%H:%M:%S)] >>> batch A: $BATCH_A" >> "$SUMMARY"
    run_batch $BATCH_A
    echo "[$(date +%H:%M:%S)] >>> batch B: $BATCH_B" >> "$SUMMARY"
    run_batch $BATCH_B
    echo "[$(date +%H:%M:%S)] >>> batch C: $BATCH_C" >> "$SUMMARY"
    run_batch $BATCH_C
  else
    # All-parallel mode -- everything in one batch.
    echo "[$(date +%H:%M:%S)] >>> all-parallel batch" >> "$SUMMARY"
    run_batch rv1 rp1 rpv1-A rpv1-B rpv1-C rv2
  fi

  # Stop conditions.
  if [ "$LOOP" != "1" ]; then
    break
  fi
  if all_done; then
    echo "[$(date +%H:%M:%S)] all scenarios reached MAX_ITERS_PER_SCENARIO" >> "$SUMMARY"
    break
  fi
  if [ "$(date +%s)" -ge "$DEADLINE" ]; then
    echo "[$(date +%H:%M:%S)] hit MAX_WALL_HRS=$MAX_WALL_HRS deadline" >> "$SUMMARY"
    break
  fi
done

# -------------------------------------------------------------------------
# Final summary footer with at-a-glance counts.
# -------------------------------------------------------------------------
{
  echo
  echo "==== overnight run finished at $(date) ===="
  pass=$(grep -c '  PASS  ' "$SUMMARY" || true)
  fail=$(grep -c '  FAIL-' "$SUMMARY" || true)
  tout=$(grep -c '  TIMEOUT  ' "$SUMMARY" || true)
  total=$(grep -cE 'iter=' "$SUMMARY" || true)
  echo "TOTAL_ITERATIONS: $total"
  echo "PASS:             $pass"
  echo "FAIL:             $fail"
  echo "TIMEOUT:          $tout"
  echo "LOG_DIR:          $OUT_DIR"
} >> "$SUMMARY"

cat "$SUMMARY"
