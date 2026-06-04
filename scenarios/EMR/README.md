# EMR scenarios

End-to-end test scenarios implementing the **EMR Testing Plan** ([EMR_TESTING_PLAN/scenarios.md](../../EMR_TESTING_PLAN/scenarios.md)).  Each scenario composes toolbox primitives into a focused regression check around one slice of the spec.

| Scenario | Topology | Phase | What it asserts | Wall time |
|---|---|---|---|---|
| [**RV-1**](RV1/) | T1 — single 3-node cluster | C | `v_62 → v_new` full schema chain + churn races + 1M-revision single doc + count-parity sweep | ~90 min (full) / ~5 min (smoke) |
| [**RP-1**](RP1/) | T2 — 1 hub + 1 filtered-pull sink (RF=3 each) | A — smoke | CV-boundary regression guard on a clean all-`v_new` topology; walks every replication-item type per-family | ~12 min (full) / ~5 min (smoke) |
| [**RPV-1**](RPV1/) | T3 — hub + 2 disjoint-filter sink clusters (9 nodes) | C | Cross-cluster rolling upgrade `v_62 → v_new` under continuous workload; 3 variants of upgrade order | ~30 min/variant (full) / ~10 min/variant (smoke) |

The matching spec entries are [`scenarios.md#rv-1`](../../EMR_TESTING_PLAN/scenarios.md), [`#rp-1`](../../EMR_TESTING_PLAN/scenarios.md), and [`#rpv-1`](../../EMR_TESTING_PLAN/scenarios.md).

---

## Quick start

### Build the `v_new` deb (once per PR)

```bash
scripts/build_ravendb_pr.sh <pr-number>
ls -lh builds/raven-pr<pr>.deb
```

### Run a single scenario (default single-lab mode)

```bash
# RV-1 (~90 min full, ~5 min smoke)
scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb

# RP-1 (~12 min full)  -- requires the legacy-counter fixture; see RP1/fixtures/README.md
scenarios/EMR/RP1/run.sh builds/raven-pr22875.deb

# RPV-1 variant A (~30 min full, ~10 min smoke)
scenarios/EMR/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb
```

Each `run.sh` does teardown → provision → install → form_clusters → scenario.

### Smoke runs (shakedown sizing — for quick PR sanity)

Each scenario's bucket counts / workload sizes can be shrunk via `-e` overrides.  See the smoke commands in each scenario's `rv1.yml` / `rp1.yml` / `rpv1.yml` header comment.  Useful pattern:

```bash
ansible-playbook scenarios/EMR/RP1/rp1.yml -K \
    -e bulk_users_sink1=100 -e bulk_orders_hub=100 \
    -e burst_active_count=100 -e burst_restore_iterations=50 \
    2>&1 | tee logs/rp1-smoke-$(date +%Y%m%d-%H%M%S).log
```

---

## Running 3 scenarios in parallel on one machine

Every scenario accepts a `cluster_id_start` offset and a `docker_network_name` override to isolate from concurrent runs on the same docker daemon.  Container names are globally unique per daemon, so each parallel run claims a disjoint cluster-id range.

**Shell A — RV-1 (cluster 1):**
```bash
scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb 1 rv1net \
    2>&1 | tee logs/rv1-$(date +%Y%m%d-%H%M%S).log
```

**Shell B — RP-1 (clusters 4-5):**
```bash
scenarios/EMR/RP1/run.sh builds/raven-pr22875.deb 4 rp1net \
    2>&1 | tee logs/rp1-$(date +%Y%m%d-%H%M%S).log
```

**Shell C — RPV-1 (clusters 7-9):**
```bash
scenarios/EMR/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb 7 rpv1net \
    2>&1 | tee logs/rpv1-$(date +%Y%m%d-%H%M%S).log
```

Three isolated docker networks; three disjoint container-name ranges; three independent teardowns scoped per-network.

| Shell | Cluster ids | Containers | Network |
|---|---|---|---|
| A | 1 | `1a/1b/1c` | `rv1net` |
| B | 4, 5 | `4a..4c` + `5a..5c` | `rp1net` |
| C | 7, 8, 9 | `7a..7c` + `8a..8c` + `9a..9c` | `rpv1net` |

Watch all three logs simultaneously:
```bash
tail -F logs/rv1-*.log logs/rp1-*.log logs/rpv1-*.log | grep ">>>>>"
```

---

## Demo / showcase mode

Pass `-e pause_between_sections=true` to any scenario to wait for ENTER before each section.  Sections always print a 3-line ASCII banner identifying themselves (visible regardless of the pause toggle) — grep-friendly via `>>>>>`:

```bash
grep ">>>>>" logs/rpv1-*.log    # table of contents
less -R +/'>>>>>  SECTION 8' logs/rpv1-*.log   # jump to a section
```

⚠ **Don't enable `pause_between_sections` for unattended runs** — any background workload already launched keeps running during the pause and accumulates state past spec.

---

## Per-scenario deep-dive

### RV-1 — Single-cluster v_62 → v_new

[`RV1/rv1.yml`](RV1/rv1.yml) — 15 sections.  Four spec-defined phases:

1. **Phase 1** — Cross-major rolling upgrade.  Seed 10k×5=50k v_62 raw-CV revisions.  Run W-1 × 4 writers in the background.  Roll 1a → 1b → 1c through the full schema chain `62000 → … → 72001`.  Stop W-1, settle, full final asserts (I-1/2/3/4/5/6/8 + schema parity at v_new).
2. **Phase 2** — Concurrent churn races.  Pre-seed 1000×30 hot docs (lands hashed-form on v_new).  W-3: 8 writers each picking a random hot doc and racing `delete → revert-from-revision → put → +attachment → -attachment` for 5 min.  Stop, drain, checkpoint.
3. **Phase 3** — 1M-revision single doc.  W-7 single-writer firehose on `users/hot` for 60 min.  Concurrent reader fetches full history every 30s; voron snapshot every 5min.  Checkpoint includes size-envelope check.
4. **Phase 4** — Count-parity sweep across the seed + hot pool + 1M-rev doc.

**Targets:** R-03, R-05, R-07.  All work happens on `<cluster_id>a/b/c` (default cluster 1).

### RP-1 — CV-boundary regression guard

[`RP1/rp1.yml`](RP1/rp1.yml) — 10 sections.  Clean all-`v_new` T2 cluster.  Workload is finite + deterministic (W-0 bulk seed + a burst).

- **W-0 seed**: 10k `users/sink1/*` + 10k `orders/hub/*` + a **per-family inventory** (one item per replication-item type under `users/sink1/family/*`).  The legacy-counter inventory item is **required** and seeded via a smuggler dump fixture — see [RP1/fixtures/README.md](RP1/fixtures/README.md) for the one-time creation procedure.
- **Burst**: 10k `users/sink1/active/*` writes + 2k delete/restore-from-revision iterations on the seed pool.
- **Asserts**: I-13 (a) (item CV shape per family), I-13 (b) (DB CV order-side via `cv_boundary_by_dbid`), I-13 (c) (replica preservation), I-7 (filter compliance), I-5/I-6 (drained, no stuck tasks via consolidated stats parity + replication dump).
- **Karmel step 8**: local update on `<sink_id>b`, verify replicates to `a` + `c`.

**Topology defaults:** hub=cluster 1, sink=cluster 2.

### RPV-1 — Cross-cluster v_62 → v_new rolling upgrade

[`RPV1/rpv1.yml`](RPV1/rpv1.yml) — 12 sections.  T3: hub + 2 disjoint-filter sinks (9 nodes).  Three variants of upgrade order:

- **Variant A — sinks first** (default): sink-1 → hub → sink-2.  Surfaces v_old-sender → v_new-receiver at Checkpoint A.
- **Variant B — hub first**: hub → sink-1 → sink-2.  Inverts to v_new-sender → v_old-receivers.
- **Variant C — interleaved**: seeded random shuffle of all 9 nodes; dwells in intra-cluster mixed-binary states.

W-1 + W-2 run continuously from T0 through endpoint on hub leader.  Sections 6/8/10 do live-write-safe light checks; section 12 (after workload stop) does the strict deep parity.  See the rpv1.yml header for variant CLI overrides.

**Topology defaults:** hub=1, sink-1=2, sink-2=3.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Could not find or access 'builds/raven-prNNN.deb'` | Typo in PR number | `ls builds/raven-pr*.deb` |
| `Cannot assign requested address` on /stats after a burst | Controller ephemeral-port exhaustion (TIME_WAIT) | The scenarios already insert a `w3_cooldown_secs` / `burst_cooldown_secs` pause; raise it if your kernel's port range is small.  Alternative: `sudo sysctl -w net.ipv4.tcp_tw_reuse=1` |
| `CountOfCounterEntries / CountOfTimeSeriesSegments MISMATCH` | Per-node-intrinsic metric (per Karmel, 2026-06-04) — NOT a real drift | Already demoted to `drift (info)` in `diagnostic_stats_parity`; flagged in a separate `INFORMATIONAL DRIFT` block but doesn't fail the run |
| `ERROR: jq is required` | `workload_w3.sh` and RP-1 burst step parse `/revisions` JSON via jq | `apt install jq` on the controller |
| `WORKLOAD DIED  pidfile=… state="NO_PIDFILE"` | A background worker exited before the scenario stopped it | If it exited because `duration_secs` ran out, ensure `duration_secs` isn't set (W-1/W-2 should run indefinitely until killed).  Otherwise inspect the workload's log under `/tmp/w*-*.log`. |
| Container name collisions on the docker daemon | Parallel run reused a cluster_id range | Each parallel run.sh invocation needs a **disjoint** `cluster_id_start` AND a unique `docker_network_name` |

---

## See also

- [EMR_TESTING_PLAN/scenarios.md](../../EMR_TESTING_PLAN/scenarios.md) — full spec (10 fields per scenario: Purpose, Topology, Initial State, Workload, Recipe, Failure Injection, Invariants, Validation Strategy, Failure Modes, Execution Notes).
- [CHEATSHEET.md](../../CHEATSHEET.md) — copy-paste commands for every toolbox tool + scenario.
- [NOTES.md](../../NOTES.md) — design rationale, environment caveats, per-tool quirks.
