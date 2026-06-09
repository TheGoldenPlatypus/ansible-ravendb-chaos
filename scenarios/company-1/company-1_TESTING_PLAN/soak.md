# company-1 Testing Plan — Soak Scenarios

Two soak scenarios cover all longevity chaos. Both fit a **2 h ceiling** (SK-2 runs twice — once per variant). Chaos cadence is elevated to make the compressed window meaningful.

| Property | Value |
|---|---|
| Duration ceiling | 2 h per scenario / variant |
| Quiescence cadence | every 30 min, 5 min chaos pause |
| Drift detector | 5-min snapshots, 15-min trend window |
| Entity scale | ≥ 10k seed, ≤ 5M ops total |
| Failure replay | seeded RNG + random-pulse chaos trace captured on failure |
| Topology | Class 2 — 1 hub + 2 sinks (T3) |

## Soak strategy

Goal: detect drift, leaks, and accumulated correctness errors over a compressed window — chaos cadence is elevated to match the shorter horizon.

### Soak run profile

- **Duration.** 2 h per scenario. SK-1 runs 2 h with interleaved chaos primitives; SK-2 runs 2 h `v_62 → v_new` rolling upgrade compressed into the window (the full schema chain).
- **Chaos cadence (SK-1).** Interleaved on a mixed-form seed — short partitions every 5–15 min, two sustained partition windows (60 min and 30 min), restarts every 15–30 min (graceful + SIGKILL mix), mentor rotations every 10–20 min, mentor-driven responsible-node flips every 25–40 min (paired `set_mentor_node` + `poll_responsible_node`), node remove + rejoin cycles at T+50 and T+95, filter mutations every 8–15 min.
- **Chaos cadence (SK-2).** Lighter to leave headroom for upgrades — partition every 15–25 min, restart every 30–40 min, mentor rotation every 25–35 min. The rolling upgrade pass spans 3 clusters over the 2 h window.
- **Workload.** W-6 baseline + W-1 bursts every 10–15 min.
- **Quiescence checkpoints.** Every 30 min, pause chaos for 5 min, run the full convergence validator suite. Convergence within 5 min required.
- **Drift detector.** Runs continuously, snapshots every 5 min, 15-min trend window. Halts the run on monotonic backlog growth + monotonic revision count without proportional workload writes.
- **Determinism.** Seeded scheduler; replay on failure.
- **Pass / fail.** All quiescence checkpoints pass + final convergence passes + no drift flagged + storage envelopes hold.

### Soak failure triage loop

1. Failure captured → artifact bundle archived.
2. Drift detector tags the first 15-min window of the trend.
3. Bisection re-run uses the seeded schedule truncated to `failure_window − 30 min`. Reproduces deterministically given the seed.
4. If not reproducible: tag as chaos flake. Otherwise: file with bundle.

## Soak scenario index

| ID | Title | Focus | Phase |
|---|---|---|---|
| **SK-1** | 2 h chaos soak with interleaved chaos primitives on a mixed-form seed | All chaos cadences in one continuous soak | D |
| **SK-2** | 2 h rolling upgrade soak `v_62 → v_new` (full schema chain) under chaos | Rolling upgrade under chaos | D |

---

<a id="sk-1"></a>
## SK-1 — 2 h chaos soak with interleaved chaos primitives

**Purpose.**
- Single 2 h longevity soak covering every chaos primitive at sustainable cadence.
- Interleaved chaos exercises the cumulative-drift surface that any single-primitive soak would miss.
- The doc-extension workload (W-2 — attachment / counter / TS add and remove) runs alongside the doc-CRUD workload (W-1) so the soak hits every entity surface; a random operational pulse replaces the per-event scheduled chaos cadences.
- Together they hit op-mixing and op-ordering interactions the deterministic catalog can't enumerate.
- Drift detector runs throughout; quiescence checkpoints every 30 min require operator presence (or scripted notification).
- Targets R-05, R-14, R-15, all convergence risks at scale.

**Topology.** Class 2 — T3 (hub + sink-1 filtered `users/*` + sink-2 unfiltered). All v_new.

**Initial State.**
- Default revisions config across all clusters.
- 30k revisions seeded on hub (mix of `users/active/*` + `users/archived/*` + `orders/*`), ~30% via raw-CV injection for mixed-form steady state — the 2 h window naturally sustains the long-lived mixed-form coexistence pattern.
- T0 quiet across all clusters.

**Workload.**

| Component | Cadence | Notes |
|---|---|---|
| W-6 baseline | continuous 2 h | low rate doc churn |
| W-1 burst | every 10 min, 2 min duration, 6 writers | mid-rate doc churn (`put`, `update`, `delete`) |
| W-3 (delete storm) | every 30 min, 1 min duration | generates revision tombstones across the filter boundary |
| W-4 (filter-boundary churn) | continuous on hub | exercises filter mutation surface |
| **W-2 (doc-extension churn)** | continuous on hub, 2 writers (reduced from default 4) | attachment / counter / TS add and remove on docs already in the pool. No doc CRUD (W-1 covers that). |
| Sink readers | continuous, 100 reads/min per sink | latency tracking |

Total ops over 2 h ≈ 90–160k. Within the 10k–5M envelope.

**Chaos schedule.**

| Event | Cadence | Notes |
|---|---|---|
| **Random operational pulse** | every 30–90 s | Seeded random pick from `{short partition (30 s – 5 min; single-node, cluster-to-cluster, or asymmetric), restart graceful, restart hard (SIGKILL), mentor rotation (hub pull task ↔ sink-1 ingest task), task ownership move, filter mutation drawn from `{users/*, users/active/*, users/active/* + orders/*, *, orders/*}`}`. Weighted toward bug-prone interactions. Full pulse trace captured for replay on failure. |
| **Partition (sustained 30 min)** | once at T+45 | a single long partition window during the soak |
| **Partition (sustained 60 min, sink offline + filter mutation mid-window)** | once from T+10 through T+70 | sink-1 partitioned for 60 min with a filter mutation at T+40; healed at T+70 |
| **Node removal + rejoin** | once at T+50, once at T+95 | gracefully remove a non-leader hub node, wait 3–5 min, rejoin. Validator suite at heal: cluster membership healthy, I-1 convergence, no orphan tasks |
| **Post-mutation spot check** | 30 s after every filter mutation drawn by the random pulse | `assert_filter_compliance` + `assert_db_cv_order_side_only` (60 s budget) |

The sustained partition windows overlap with the random pulse — while sink-1 is partitioned for 60 min, the random pulse keeps firing against the rest of the cluster.

**Scenario Recipe.**

| # | Step |
|---|---|
| 1 | Provision T3 all-v_new; seed mixed-form 30k revs; T0 quiet |
| 2 | Capture T0 baseline (full artifact bundle) |
| 3 | Start workloads + drift detector |
| 4 | Run the interleaved chaos schedule (table above) for 2 h |
| 5 | Quiescence at T+30, T+60, T+90: pause chaos 5 min, wait for replication to drain (5 min budget), full validator suite, artifact bundle, resume |
| 6 | T+120 final: stop all workloads, full quiescence (10 min budget), validator suite + drift report |
| 7 | Teardown |

**Failure Injection.** Per the chaos schedule above. The random pulse fires roughly 80–240 events across the 2 h (mix of short partitions, restarts graceful + hard, mentor rotations, task moves, filter mutations). Plus 1 × 60-min sustained partition + 1 × 30-min sustained partition + 2 node remove / rejoin cycles (these stay scheduled).

**Expected Invariants.** I-1..I-7 + I-11 at every quiescence checkpoint. I-8 voron envelope (whole-DB `/stats.SizeOnDisk`). I-9 no sustained backlog growth across any 30-min window. **I-13** at every quiescence (the new lane stays correct through 2 h of cumulative chaos). I-7 post every filter-mutation spot check (no leak, no skip).

**Validation Strategy.** Per-quiescence retry budget (60 s `LAG` extension, then `STUCK` fails). Drift detector: any 15-min monotonic backlog or revision-count growth without proportional workload growth halts the run. Sustained partition phases (60 min and 30 min) get explicit recovery validators when they heal — backlog drain must complete within 50 min of heal for the 60-min partition, 30 min for the 30-min partition.

**Expected Failure Modes.**
- Slow tombstone-cleanup divergence under continuous filter mutation → tombstone parity widens (I-1 + I-2) across quiescence checkpoints.
- Touch-path miss under partition + restart sequence → I-3 (proxy) catches at the next quiescence.
- Sustained partition window doesn't drain within budget after heal → I-5 fails.
- Cumulative orphan revision growth from narrow → widen cycles across 2 h → I-4 catches.
- Filter-matching writes silently dropped at any spot check → I-13 + I-7 fail.

**Execution Notes.** Wall 2 h. `Solo`. Phase D. Operator presence at quiescence checkpoints or scripted notification on validator failure. Drift detector halts autonomously on trend detection.

---

<a id="sk-2"></a>
## SK-2 — 2 h rolling upgrade soak (`v_62 → v_new`)

**Purpose.**
- Compressed rolling upgrade across a 3-cluster topology within 2 h with chaos throughout.
- All 9 nodes go from `v_62` to `v_new` through the full schema chain.
- Targets R-01, R-07, R-08.

**Topology.** Class 2 — **T3** cross-cluster (disjoint filters: sink-1 `users/*`, sink-2 `orders/*`). All 9 nodes start on `v_62`, end on `v_new`.

**Initial State.**
- Default revisions config across all clusters (per-collection rules: tight on `Users`, moderate on `Orders`, loose on `Internal`).
- Seeded with the 7-bucket prefix convention: ~3k each of `users/sink1/{i}`, `users/sink2/{i}`, `users/hub/{i}`, `orders/sink1/{i}`, `orders/sink2/{i}`, `orders/hub/{i}`, plus ~2k `Internal/{i}` = 20k revisions in `v_62` raw-CV form.
- T3 filters wired (sink-1 = `users/sink1/* + orders/sink1/*`; sink-2 = `users/sink2/* + orders/sink2/*`).
- Replication wired and quiet.

**Workload.**
- W-6 baseline.
- W-1 bursts every 15 min.
- W-2 bursts every 20 min.
- All partitioned across the 7 prefix buckets so the filter geometry is exercised throughout the 2 h soak.

**Chaos schedule** (light, to leave headroom for upgrades — plus one sustained outage, below, that deliberately overlaps the cross-version window).

| Event | Cadence |
|---|---|
| Partition (rolling, short) | every 15–25 min |
| Restart | every 30–40 min |
| Mentor rotation | every 25–35 min |
| **Sustained sink-2 outage (long-outage + cross-version replay, R-01)** | once — sink-2 isolated from hub T+35 → T+70 (~35 min), while hub is `v_new` and sink-2 is still `v_62`; healed and drained before sink-2's own upgrade (see timeline) |

**Upgrade timeline.**

| T (min) | Action |
|---|---|
| 0 | Start workloads + drift detector + chaos. All nodes at `v_62`. |
| 20 | Begin hub upgrade. Pause chaos 2 min before each node upgrade. Upgrade `1a → 1b → 1c` with 5 min gaps. Mid-roll checkpoint I-2 (CV), I-3 (proxy), I-4 after each node. |
| 35 | Hub now `v_new`; both sinks still `v_62` → **cross-version legs active**. **Partition sink-2 from hub** (long outage begins). Hub keeps writing `users/sink2/*` + `orders/sink2/*`, so a backlog accumulates against a `v_new` sender / `v_62` receiver. |
| 50 | Begin sink-1 upgrade (3 nodes over ~15 min). sink-2 remains partitioned. |
| 70 | **Heal sink-2.** It replays the ~35-min backlog across the cross-version leg (`v_new` hub → `v_62` sink-2). **Recovery validator:** backlog drains within a 10 min budget (by ~T+80); per-prefix count parity on `users/sink2/*` + `orders/sink2/*`; I-7 (no leak, no skip) holds across the long-outage cross-version replay (R-01). |
| 80 | Begin sink-2 upgrade — **only after the post-heal replay has drained** (recovery validator green). |
| 105 | All nodes now v_new. Hub ↔ sink-1 connection now naturally on the new lane (both peers v_new). |
| 105–115 | Write a fresh burst spanning all 7 buckets (~150 each) to hub; wait for drain. Run `assert_db_cv_order_side_only` + `assert_stored_item_cv_split` on a sample. **Filter geometry assertion** on the burst: sink-1 has only `users/sink1/*` + `orders/sink1/*`; sink-2 has only `users/sink2/*` + `orders/sink2/*`; hub-only buckets stay on hub. Post-upgrade smoke CV + filter check. |
| 115–120 | Post-full-upgrade + post-lane-transition soak window. |
| 120 | Final quiescence: full validator suite + schema-version parity (`expected_version=72001`) + CV-boundary spot checks + final filter geometry. |

**Failure Injection.** 9 node upgrades + chaos baseline + one sustained sink-2 outage (T+35 → T+70) in the cross-version window.

**Expected Invariants.** I-1..I-7 + I-11 at every quiescence (filter compliance per prefix bucket included throughout). I-8. I-10 mixed during, converged after. Full `62000 → ... → 72001` schema chain runs to completion on every node. **Post-heal (T+70):** sink-2's long-outage backlog drains within budget and `users/sink2/*` + `orders/sink2/*` reach per-prefix count parity with the hub — replay across a `v_new`→`v_62` leg loses nothing and leaks nothing (I-7, R-01). Post-T+110: I-13, I-7 on the post-lane-transition spot check (filter geometry survives the lane transition).

**Validation Strategy.** Each upgrade step pauses chaos 2 min and waits for replication to drain (5 min budget) before its mid-roll checkpoint, so checkpoint state is steady, not transient. Per-checkpoint retry budget: 60 s `LAG` extension, then `STUCK` fails. The lane-inert validator runs at every mid-roll checkpoint that has a cross-version peer present; the new-lane validators (I-13) run only at and after the T+105 full-convergence point (the lane must not activate before every peer is v_new). Endpoint gate is mechanical: schema-version parity (`expected_version=72001` on all 9 nodes via PC-1) **and** the T+110 lane-transition smoke (a fresh burst spanning all 7 buckets drains, then `assert_db_cv_order_side_only` + `assert_stored_item_cv_split` pass on the sample, with full filter geometry). Drift detector runs throughout; any 15-min monotonic backlog or revision-count growth without proportional workload halts the run. The sustained sink-2 outage gets an explicit recovery validator at heal (T+70): backlog must drain within a 10 min budget (by ~T+80), and sink-2's per-prefix counts must reach parity with the hub before sink-2's own upgrade is allowed to start — the drift detector's growth alarm is suspended for sink-2's leg only during the partition window (a planned outage is not drift).

**Expected Failure Modes.**
- Schema chain `62000 → ... → 72001` stalls mid-step on a node, or a restart mid-chain fails to resume → I-10 mismatch at the next mid-roll checkpoint.
- New lane activates while a v_62 peer is still present (premature lane switch) → lane-inert validator fails before T+105.
- Cluster reaches all-v_new but filtered connections fail to switch to the new lane → I-13 fail at the T+110 smoke despite full convergence.
- Filter geometry breaks under cross-version replication during the roll: a sink-2 bucket leaks to sink-1, or a sink-1 bucket is skipped → I-7 (filter invariants) fail at a mid-roll checkpoint.
- A node never reaches `72001` (upgrade silently incomplete) → endpoint schema-parity gate fails.
- Long-outage cross-version replay (T+70 heal): sink-2's 35-min backlog fails to drain within budget, or the `v_new` hub → `v_62` sink-2 replay drops or duplicates items, or leaks a wrong-prefix doc → recovery validator fails on per-prefix count parity / I-7 (R-01).

**Execution Notes.** Wall 2 h. `Solo`. Must run before each release. The smoke check at T+110 confirms the cluster's filtered connections switch to the new lane once the cluster fully converges on v_new. Phase D.
