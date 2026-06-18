# company-1 Testing Plan — Scenario Catalog

Soak scenarios live in [soak.md](soak.md). Every scenario block uses the same 10 fields, in this order: **Purpose**, **Topology**, **Initial State**, **Workload**, **Scenario Recipe**, **Failure Injection**, **Expected Invariants**, **Validation Strategy**, **Expected Failure Modes**, **Execution Notes**.

Entity scale floor 10k; ceiling 5M. Hard scenario duration ceiling 2 h.

## Test catalog

Full detail (recipe, invariants, failure modes) is in each scenario's section below; soak scenarios are in [soak.md](soak.md).

| ID | What it covers | Topology | Phase |
|---|---|---|---|
| [**RV-1**](#rv-1) | **Revision migration** — full schema chain, then churn races, then 1M-revision history, then a count-parity sweep | Single cluster, 3 nodes | C |
| [**RV-2**](#rv-2) | **Sharded minimum** — internal replication + sharded → non-sharded | Sharded source (3 shards × 3 replicas) + 1 non-sharded target | C |
| [**RV-3**](#rv-3) | **Extended sharded** — import / ETL / backup / Revisions Subscriptions | Sharded source (3 shards × 3 replicas) + 3 non-sharded targets | C |
| [**RP-1**](#rp-1) | The **CV-boundary** regression guard (fix #1 — stored-CV shape on a v_new receiver) | 1 hub cluster → 1 filtered sink cluster, Sink RF 3 | A |
| [**RP-2**](#rp-2) | **Filter-mutation / conflict / cross-sink-isolation** chaos — bidirectional, four sequential phases | 1 hub ↔ 2 sink clusters, bidirectional + overlapping filters, RF 3 | B |
| [**RP-3**](#rp-3) | The **failover-cursor** lifecycle (fix #2 — replication position survives crashes) | 1 hub cluster ↔ 1 sink cluster, Hub & Sink RF 3 | B |
| [**RPV-1**](#rpv-1) | **Cross-cluster rolling upgrade** + filter geometry under version mix | 1 hub + 2 sink clusters, disjoint filters, 9 nodes | C |
| [**RPV-2**](#rpv-2) | **Backup / restore** lifecycle — snapshot vs smuggler PK form + restored-sink resume | 1 hub → 1 sink cluster (sink RF 3) + 2 restore-target clusters per round | C |
| [**SK-1**](soak.md#sk-1) | **Longevity** under continuous chaos (cumulative drift) | 1 hub + 2 sink clusters, filtered + unfiltered, all v_new | D |
| [**SK-2**](soak.md#sk-2) | **Longevity** across a rolling upgrade (drift through the version transition) | 1 hub + 2 sink clusters, disjoint filters | D |

## Execution sequence

| Phase | Scenarios (in order) | Est time | Unlocks |
|---|---|---|---|
| **A — Smoke** | RP-1 | ~12 min | Basics are sane — the CV-boundary fix holds on a clean cluster. Any `CORRUPT` → stop. |
| **B — Filtered-replication fix** | RP-2 → RP-3 | ~100 min | The CV-boundary + failover-cursor fix is regression-clean (Tier 1). |
| **C — Migration & rolling upgrade** | RV-2 → RV-3 → RPV-2 → RPV-1 → RV-1 | ~290 min | The PK migration is correct end-to-end, incl. cross-version + rolling upgrade (Tier 2). |
| **D — Soak** | SK-1 → SK-2 | 2 h + 2 h | Release-ready (Tier 3). |

Smoke + fix regression (A + B) ≈ **1 h 50 min**; the full focused pass (A + B + C) ≈ **6 h 40 min**. 

---

## Class 1 — Single Cluster

<a id="rv-1"></a>
### RV-1 — `v_62 → v_new` full chain + churn races + 1M-revision single-doc + count-parity sweep

**Purpose.** Single Class 1 mega-scenario exercising the revision PK migration code from three angles on one continuous cluster lifecycle.
- Phase 2's race conditions land on data that just migrated through phase 1's full schema chain (more meaningful than churn on a clean v_new cluster).
- Phase 3 puts the migrated + churned mixed-form database under a 1M-revision single-doc stress to surface storage envelope or read-latency regressions.
- Phase 4 closes with a full-dataset count-parity sweep across the migrated v_62 data plus the 1M-revision history.

Targets R-03, R-05, R-07.

**Topology.** Class 1, T1 — single 3-node cluster on v_62 at start, upgraded to v_new through phase 1.

**Initial State.**
- All 3 nodes `v_62` (schema 62000).
- Database `db1` with a **keep-all revisions configuration** (override of the plan's default — no purge limits; phase 3 needs the 1M-revision history to accumulate).
- 10k docs × 5 revs = 50k revisions seeded in v_62 raw-CV form.
- A separate pool of 1000 hot docs prepared for phase 2 (re-seeded after upgrade).

**Workload.** Multi-phase per the recipe.

**Scenario Recipe.**

| # | Phase | Step | Wall |
|---|---|---|---|
| 1 | 1 — cross-major rolling upgrade | Provision v_62 3-node cluster; form; create `db1`; seed 50k v_62 revs | — |
| 2 | 1 | Baseline checkpoint: schema 62000 across all nodes | — |
| 3 | 1 | Start W-1 at 4 writers | — |
| 4 | 1 | Upgrade `1a` to v_new through the full schema chain `62000 → ... → 72001`; wait for member | — |
| 5 | 1 | Checkpoint after `1a`: I-2 (CV), I-3 (proxy), I-4, I-10 (1a 72001, 1b/1c 62000); `assert_node_and_feature_parity` with `allow_mixed=true` | — |
| 6 | 1 | Upgrade `1b`; wait for member; checkpoint | — |
| 7 | 1 | Upgrade `1c`; wait for member; stop W-1; quiet | — |
| 8 | 1 | Phase-1 final: I-1, I-2, I-3, I-4, I-5, I-6, I-8, schema parity at 72001 | ~14 min |
| 9 | 2 — concurrent churn races | Pre-seed 1000 hot docs × 30 revs (in addition to the v_62-migrated set); the hot docs land as v_new hashed form | — |
| 10 | 2 | Start an 8-writer concurrent loop on the 1000 hot docs: each writer picks a random doc and runs `delete → revert-from-revision → put → add attachment → remove attachment` for 5 min. Each write creates a new revision on the just-migrated docs, exercising the legacy raw-CV → hashed re-keying path (R-03) — any missed re-key surfaces as a duplicate at the phase-2 checkpoint | — |
| 11 | 2 | Stop writers; wait for replication to drain | — |
| 12 | 2 | Phase-2 checkpoint: I-1, I-2, I-3 (proxy), I-4 (adopt-path probe). The hot-doc set must show no duplicate rows and no orphans | ~7 min |
| 13 | 3 — large-history single doc | Start single-writer W-7 on `users/hot`: 16k revs/min target for 60 min → 1M revisions | — |
| 14 | 3 | Concurrent reader fetches full revision history every 30 s; record p99 read latency | — |
| 15 | 3 | Every 5 min: capture voron stats, revision count, p99 read latency | — |
| 16 | 3 | After 60 min: stop writer; quiet | — |
| 17 | 3 | Phase-3 checkpoint: I-2 (count = 1M per peer), I-3 (proxy at this scale), I-4, I-8 (voron envelope), no monotonic latency growth > 2× baseline | ~60 min |
| 18 | 4 — count-parity sweep | Sparse-sample composition (workload trace ↔ `/revisions?id=`) across the full migrated v_62 dataset + the 1M-revision history: per-doc revision counts on a deterministic sampled probe set equal the workload-trace expectation on every node. (Per-doc count parity has no scalable enumeration surface — sampling is the only viable form.) | — |
| 19 | 4 | Phase-4 checkpoint: I-3 across the full dataset | ~5 min |
| 20 | finish | Final voron stats; envelope check across the whole cluster lifetime | — |

**Failure Injection.** None — the workload composition is the stress.

**Expected Invariants.**

| Phase | Checks |
|---|---|
| 1 | I-1, I-2, I-3, I-4, I-5, I-6, I-8, I-10 (cross-major schema chain) |
| 2 | I-1, I-2, I-3 (proxy), I-4 (adopt probe). Phase 2 specifically tries to provoke duplicate rows via concurrent `delete + revert + put`; the proxy catches the symptom (per-doc count drift). |
| 3 | I-2 (count = 1M), I-3 (proxy at scale), I-4, I-8 (voron within envelope) |
| 4 | I-3 across the union dataset (full-dataset count parity) |

**Validation Strategy.** Per-phase checkpoint blocks the next phase if invariants fail. Phase 4's sweep is the cumulative count-parity check across the whole cluster's history. Read latency in phase 3 must remain bounded over time (no monotonic > 2× baseline).

**Expected Failure Modes.**
- Phase 1: schema chain stalls on a node; checkpoint detects via I-10.
- Phase 2: `RevisionKey` delete-old-and-insert-hashed not atomic under concurrent writers → per-doc count drift → phase 2 proxy catches at the checkpoint. Or a write path leaves a document's legacy raw-CV revision row unmigrated alongside the new hashed row (R-03) → duplicate raw + hashed rows → same proxy count drift.
- Phase 3: monotonic voron growth past envelope (I-8), or read-latency regression past 2× baseline.
- Phase 4: per-doc count parity violation anywhere in the migrated + churned + high-history dataset.

---

## Class 2 — Hub-Sink Replication

<a id="rp-1"></a>
### RP-1 — CV-boundary regression guard

**Purpose.**
- Regression guard for the CV-boundary fix on a clean all-`v_new` topology.
- From a single captured Sink state, asserts I-13 + I-7 + I-5 + I-6.
- Walks every replication-item type (documents, tombstones, attachments, counters, time-series, conflict markers, revisions, etc.) as it lands on the sink leader.
- Then confirms the captured state is correctly preserved across the sink's internal replicas.

**Topology.** Class 2 — T2 (1 hub + 1 sink filtered pull), Sink RF = 3. All nodes v_new.

**Initial State.**
- Default revisions config.
- Hub and Sink quiet.
- sink-1 filter `users/sink1/*`.
- Hub seeded with 10k `users/sink1/*` + 10k `orders/hub/*` + one item of each replication-item type under `users/sink1/family/*` (document, document tombstone, conflict document, revision document, revision tombstone, attachment metadata, attachment stream, attachment tombstone, counter group, time-series segment, deleted time-series range, legacy counter).

**Workload.**
- W-0 deterministic bulk seed at start.
- During the scenario: 10k `users/sink1/active/{i}` writes + 2k `delete / restore-from-revision` ops on the seeded set.
- Finite, deterministic.

**Scenario Recipe.**

| # | Step |
|---|---|
| 1 | Provision T2 with Sink RF = 3, all v_new; wire filtered pull |
| 2 | Seed Hub with the bulk 20k + per-family inventory; wait for filtered pull + Sink internal replication to drain |
| 3 | Capture baseline CV snapshot per node (Hub + Sink group SA / SB / SC) |
| 4 | Execute the 10k `users/sink1/active/*` writes + 2k `delete / restore` burst |
| 5 | Wait for replication to drain end-to-end |
| 6 | Capture post-batch CV snapshot |
| 7 | Validate the CV invariants below |
| 8 | Locally update one item per family on SB; verify both halves of the stored CV carry the local update + replicate to SA / SC |

**Failure Injection.** None — the workload is the test.

**Expected Invariants.**

| ID | Check |
|---|---|
| **I-13 (a)** item CV split | Every item on Sink (bulk + per-family) passes `assert_stored_item_cv_split` |
| **I-13 (b)** DB CV order-side | SA / SB / SC `LastDatabaseChangeVector` passes `assert_db_cv_order_side_only`. The load-bearing regression guard. |
| **I-13 (c)** replica preservation | Stored item CV identical across SA / SB / SC |
| I-13 (d) conflict | Spot check conflict detection (deeper check lives in RP-2 phase (a)) |
| I-7 | Every Sink doc matches `users/sink1/*`; no source `users/sink1/*` doc missing on Sink; no `orders/hub/*` leaked through |
| I-5, I-6 | Drained + no stuck task |

**Validation Strategy.** I-13 (b) is the load-bearing assertion across every Sink-group node. I-13 (a) walks the per-family inventory. I-13 (c) cross-checks SA / SB / SC stored CVs.

**Expected Failure Modes.**
- I-13 (b) fails on any Sink-group node → DB CV not correctly maintained on the sink.
- I-13 (a) fails on a specific item type → the receive path didn't handle that type correctly.
- I-13 (a) / (c) fail on SB or SC but pass on SA → internal replication inside the sink dropped or rewrote the captured state.

**Execution Notes.** Wall ~12 min. Phase A. Smoke — always run. All peers must be v_new for the scenario to be meaningful.

---

<a id="rp-2"></a>
### RP-2 — Compound replication chaos

**Purpose.**
- Bidirectional-filtered mega-scenario sequenced across four phases:
    - **(a)** conflict + echo + cross-sink isolation
    - **(b)** filter mutation under chaos
    - **(c)** mentor rotation + leader kill
    - **(d)** split brain
- Each phase lands on residual state from the previous, so cumulative regressions surface.

**Filter contract reminder.** Filter mutations apply forward only — no re-send on widen, no cascade delete on narrow.

**Topology.** Class 2 — T3 (bidirectional filtered pull on hub ↔ sink-1; sink-2 wired with an overlapping pull for cross-sink isolation checks). Hub RF = 3, sink-1 RF = 3, sink-2 RF = 3 so both sides can fail over. All v_new.

**Initial State.**
- Default revisions config.
- T3 wired and quiet.
- Hub and sink-1 each seeded with 5k `users/*` docs of their own.
- sink-2 (overlapping filter) wired in.

**Workload.**
- Per phase.
- W-4 (filter-boundary churn) is the baseline once chaos begins.
- W-1 / W-5 take over during specific sub-phases.

**Scenario Recipe.**

| # | Phase | Sub-phase | Step |
|---|---|---|---|
| 1 | setup | — | Provision T3 — Hub + sink-1 + sink-2, all v_new, Hub RF = 3, Sink RF = 3, bidirectional filtered pull on each leg |
| 2 | setup | — | Seed Hub and sink-1 each with 5k `users/*` of their own; drain |
| 3 | **(a)** echo + conflict + cross-sink | echo prevention | Hub writes 1k `users/active/*`; drain. Confirm sink-1 received the 1k; confirm Hub's *incoming* count from sink-1 is zero for those docIds (no echo) |
| 4 | (a) | conflict + cross-sink | Concurrent writes: Hub writes `users/conflict/{k}` (k ∈ [0..1000]); sink-1 writes the same docIds with distinct bodies. sink-2 writes a different 1k `users/leak-check/*` (only on sink-2). |
| 5 | (a) | drain + validate | Drain across all three peers. Validate: conflicts resolved on Hub + sink-1 (single winning chain); sink-1 did NOT receive `users/leak-check/*` (no cross-sink leak) |
| 6 | **(b)** filter mutation | widen + narrow with backlog | Generate backlog: ensure sink-1 backlog > 1k items. Widen sink-1 filter to `users/*, orders/*`. W-4 for 60 s. Narrow back to `users/*`. W-4 for 60 s. Validate forward-only contract |
| 7 | (b) | overlapping filters + partition | Reconfigure sink-2 filter to `users/*` (overlap). W-1 on Hub. Mid-window: partition sink-2 from Hub for 90 s; heal. Per-prefix revision-count assertion |
| 8 | (b) | narrow with backlog | Reset sink-1 filter to `users/active/* + users/archived/*`. Seed 5k `users/active/*` + 5k `users/archived/*` on Hub; drain. Snapshot sink-1. Narrow sink-1 to `users/active/*`. W-1 touching both for 2 min. Validate: archived snapshot preserved; new active writes reach sink-1 (I-7 + I-13 regression guard) |
| 9 | (b) | reconnect after mutation during partition | Partition sink-1 from Hub entirely. Change sink-1 filter to `users/active/*`. Wait 90 s with Hub writing both prefixes. Heal. Validate: post-heal sink-1 sees only `users/active/*` writes from the partition window |
| 10 | (b) | out-of-order injection | Inject 2000 ms egress delay on hub node `1a`. W-1 distributed across all 3 Hub nodes. After 60 s, clear lag. Drain. Validate I-2 CV multiset equality |
| 11 | (b) | sink-mentor restart | W-1 on Hub. Restart Sink mentor `2a` four times over 5 min (wait-for-member between each). Validate I-2, I-3 (proxy), I-6 |
| 12 | (b) | retry storm | W-1 on Hub. For 5 min, every 15 s cut TCP between `1a` and `2a` for 5 s then restore. Validate I-3 (proxy); reconnects > 15 but no duplicates |
| 13 | **(c)** mentor rotation + leader kill | hub-side mentor rotation | W-1 baseline. At intervals: rotate Hub mentor → `1b` → `1c` → `1a` (3 rotations over 4 min) |
| 14 | (c) | sink-side mentor rotation | At intervals: rotate sink-1 mentor → `2b` → `2c` → `2a` (3 rotations over 4 min) |
| 15 | (c) | rotation checkpoint | I-2, I-3 (proxy), I-6 — replication resumes within 30 s of every rotation |
| 16 | (c) | hub leader kill | Hard-kill Hub leader (`1a`) — chaos to disrupt the in-flight replication stream. Wait for cluster health. W-1 continues 2 min against the new leader. Restart `1a`; wait for `1a` to rejoin as full member |
| 17 | (c) | sink leader kill | Hard-kill Sink leader (`2a`). Wait for cluster health. W-1 continues 2 min. Restart `2a`; wait for `2a` to rejoin as full member |
| 18 | (c) | node remove + rejoin | Gracefully `remove_node` a non-leader sink-1 replica while the hub keeps streaming; wait 1–2 min (the hub builds a backlog for the shrunken sink group); `add_node` to rejoin and wait for full member. Assert the rejoined node back-fills the missed window and converges — **membership change under active replication (R-15)** |
| 19 | (c) | kill checkpoint | I-1, I-2, I-3, I-4, I-5, I-6, I-11 across the leader-kill + remove/rejoin window; the rejoined node back-fills within budget |
| 20 | **(d)** split brain | partition | W-5 conflict generator: writers pinned to `1c` (minority) and `1a` (majority). Isolate `1c` from `1a` / `1b`. Run 3 min with concurrent writes on both sides |
| 21 | (d) | heal | Heal `1c`. Stop W-5. Wait for replication to drain |
| 22 | (d) | checkpoint | I-11 conflict resolution converges within 90 s of heal. I-7 filter compliance. I-1 + I-2 tombstone parity post-heal |
| 23 | finish | — | Final convergence across all three peers: I-1, I-2, I-3, I-4, I-5, I-6, I-7, I-11, I-13 |

**Failure Injection.**

| Phase | Primitives |
|---|---|
| (a) | Bidirectional concurrent writes producing conflicts; cross-sink overlap workload |
| (b) — 7 sub-phases | Widen, narrow, partition (sink-2), narrow with backlog, partition + filter mutation, egress lag, sink-mentor restart × 4, TCP flap × 20 |
| (c) | 6 mentor rotations (3 hub + 3 sink); 2 hard kills + 2 restarts (1 per side); 1 node remove + rejoin (sink-1 replica) |
| (d) | 1 asymmetric partition + concurrent writes + heal |

Cumulative ~20+ chaos events. Each phase lands on residual state from the previous — split brain (d) runs on a cluster that has just absorbed leader kills (c) that ran on a cluster that just absorbed the 7 filter-mutation events (b) on top of a bidirectional setup with conflicts (a).

**Expected Invariants.**

| Phase | Anchor invariants |
|---|---|
| (a) | I-13 (d) (echo + conflict checks) + I-13 (a) (item CV shape preserved across resolution), I-11 (resolution converges), I-7 (no cross-sink leak) |
| (b) | I-7 (no leak, no skip — the load-bearing regression guard sits on I-13 (b)), I-1 + I-2 (tombstone parity), I-2 (out-of-order CV convergence), I-3 (proxy under retry storm), I-6 (no stuck task) |
| (c) | I-1, I-2, I-3, I-4, I-5, I-6, I-11 — after each rotation/kill the cluster returns to health and replication resumes within 30 s (consensus recovery itself is out of scope — Raft is unchanged); a removed sink replica back-fills its missed window within budget after rejoin |
| (d) | I-11 conflict resolution converges within 90 s of heal; I-7 maintained throughout; I-1 + I-2 tombstone parity post-heal |
| final | I-1, I-2, I-3, I-4, I-5, I-6, I-7, I-11, I-13 across all three peers |

**Validation Strategy.** Per-phase checkpoint blocks the next phase if its invariants fail. Forensic capture at each phase boundary. Workload-trace replay is the source of truth for filter-mutation projection (phase (b) sub-checks). Conflict resolution validator runs with 90 s `LAG` budget post-heal events (phases (a), (c), (d)).

**Expected Failure Modes.**
- Phase (a): echo prevention misidentifies an echo → loop / duplicates. Cross-sink isolation breaks → `users/leak-check/*` on sink-1.
- Phase (b): widen back-fills previously-filtered-out docs; narrow deletes already-replicated archived docs; filter-matching writes silently dropped (the silent-skip bug class); out-of-order arrivals cause CV multiset mismatch; mentor restart loses in-flight batch; retry storm produces duplicates.
- Phase (c): cumulative mentor-rotation regression (works first 3 rotations, breaks on the 6th); killed leader's in-flight batch lost or re-sent; a removed-then-rejoined sink replica fails to back-fill the missed window (R-15 membership change).
- Phase (d): conflict-resolution divergence between minority and majority sides; filter compliance breaks during heal-replay.

---

<a id="rp-3"></a>
### RP-3 — Full failover-cursor lifecycle

**Purpose.**
- Six injection points exercise the durable-cursor lifecycle on shared setup:
    - handshake on first connect
    - receiver crashes before and after the cursor write
    - sender crashes before and after the receiver's cursor write
    - a no-mutation completed-scan advance
- Each sub-phase asserts the failover invariant (I-12) at a specific cursor-protocol point.

**Topology.** Class 2 — T2 with Hub RF = 3 AND Sink RF = 3 (both sides can fail over). All v_new — required for the cursor to be active.

**Initial State.**
- Default revisions config.
- Empty cluster pair on first run; no prior cursor records.
- Pre-prepared write batches for each sub-phase.

**Workload.** Hub writes batched per sub-phase (~5k per batch).

**Scenario Recipe.**

| # | Phase | Step |
|---|---|---|
| 1 | setup | Provision T2 with Hub RF = 3 and Sink RF = 3, all v_new; confirm cursor key absent |
| 2 | **(a)** handshake | Hub writes 5k `users/*`; wait for filtered pull + sink internal replication to drain |
| 3 | (a) | Inspect durable cursor — must exist now, value = Hub source frontier C1 |
| 4 | (a) | Disconnect + reconnect Sink task; next handshake presents C1 as `SinkCanStartFromChangeVector`; sender starts scan from C1 (not from etag 0) |
| 5 | (a) | Phase-(a) checkpoint: I-12 — handshake presents C1; a missing cursor is written then advances |
| 6 | **(b)** receiver crash BEFORE cursor write | Hub flushes second 5k batch. Wait for Sink-leader to accept items but BEFORE the cursor write fires — hard-kill Sink-leader. Wait for Sink leader election. Inspect cursor on new owner — must equal C1 (not advanced). Sender reconnects, resends from C1; cursor advances to C2 |
| 7 | (b) | Phase-(b) checkpoint: I-12 — no premature durable advance; cursor advances only after the resend |
| 8 | **(c)** receiver crash AFTER cursor write | Hub flushes third 5k batch. Wait until Sink-leader completes the cursor write to C3. Then hard-kill Sink-leader. Wait for election. Inspect cursor on new owner — must equal C3. Sender resumes from C3; no double-application |
| 9 | (c) | Phase-(c) checkpoint: I-12 (already-merged branch on resend), no duplicates |
| 10 | **(d)** sender crash BEFORE receiver cursor write | Hub flushes fourth 5k batch. Wait for Sink-leader to accept items but before its durable write — at the same instrumented moment, hard-kill Hub-leader. New Hub leader elects. Record observed cursor value (C3 or C4). New Hub leader resumes scan from whichever cursor Sink wrote. Eventual cursor = C4 |
| 11 | (d) | Phase-(d) checkpoint: cursor either C3 (no advance) or C4 (advance) — both valid; no duplicates; no stuck task |
| 12 | **(e)** sender crash AFTER receiver cursor write | Hub flushes fifth 5k batch. Sink-leader completes its cursor write to C5. Then hard-kill Hub-leader. New Hub leader resumes from C5 (read at handshake). No re-send of batch-5 items already acked at C5 |
| 13 | (e) | Phase-(e) checkpoint: I-12 — handshake reads C5; no double-application |
| 14 | **(f)** no-mutation completed scan | Hub writes 1k `orders/*` (all filtered OUT by Sink). Wait for the sender's all-filtered completed-scan frame to land on Sink with proof-barrier etag = Sink's current DB etag |
| 15 | (f) | Inspect cursor — must advance to the Hub frontier covering the 1k `orders/*` writes despite zero item-effect on Sink. Cursor = C6 |
| 16 | (f) | Phase-(f) checkpoint: I-12 — Sink confirms progress before the cursor advances; the advance branch fires |
| 17 | finish | Final convergence: cursor monotonically advanced C0 → C1 → C2 → C3 → C4 → C5 → C6; ack count matches workload trace; no I-2 / I-13 regressions |

**Failure Injection.** Two Sink-leader kills (timed to before / after cursor write), two Hub-leader kills (timed to before / after Sink's cursor write), one all-filtered no-mutation completed scan.

**Expected Invariants.** I-12 across the cursor lifecycle — cursor read on every takeover, no premature advances in phases (b) and (d), and correct cursor-write semantics across all five resumes plus the no-mutation advance. No I-2 / I-13 regressions across the whole 25k-write window.

**Validation Strategy.** `inspect_durable_cursor` at every branch point. Per-phase checkpoint asserts the phase-specific invariant before the next phase. Cursor history forms a monotonic chain C0 → C6.

**Expected Failure Modes.**
- Phase (b): cursor advances despite the kill before write → I-12 fails.
- Phase (c): resend causes duplicates → I-12 already-merged branch broken.
- Phase (d): Sink's durable write proceeds without sender's ack → cursor advanced without proof.
- Phase (e): new Hub leader doesn't observe C5 at handshake → resends already-acked items → duplicates.
- Phase (f): all-filtered scan doesn't advance the cursor → cursor stalls on empty windows.

**Execution Notes.** Wall ~25 min. Phase B.

---

<a id="rpv-1"></a>
### RPV-1 — Cross-cluster `v_62 → v_new` rolling upgrade (3 variants, filter-aware)

**Purpose.**
- Walk all 9 nodes from `v_62` to `v_new` across the T3 cross-cluster topology under doc + extension-op churn.
- Three variants differ only in the upgrade order — together they cover both directions of the cross-version surface (`v_62 sender → v_new receiver` and `v_new sender → v_62 receiver`) plus the interleaved mid-roll cluster state.
- Filter geometry is asserted at every checkpoint: the workload is partitioned by ID prefix so each sink should receive a known subset, and unmatched IDs should stay on hub.
- Targets R-01, R-07, R-08.

**Topology.** Class 2 — T3 cross-cluster (Hub + sink-1 + sink-2, 9 nodes total). Disjoint filters:
- sink-1 filter: `users/sink1/* + orders/sink1/*`
- sink-2 filter: `users/sink2/* + orders/sink2/*`
- Unmatched: `users/hub/*`, `orders/hub/*`, and the entire `Internal` collection stay on hub only

All 9 nodes start `v_62`.

**Initial State.**
- Default revisions config.
- Hub seeded with 7 prefix buckets in `v_62` raw-CV form: 2k each of `users/sink1/{i}`, `users/sink2/{i}`, `users/hub/{i}`, `orders/sink1/{i}`, `orders/sink2/{i}`, `orders/hub/{i}`, plus 3k `Internal/{i}` = 15k revisions.
- T3 wired (sink-1 filter `users/sink1/* + orders/sink1/*`; sink-2 filter `users/sink2/* + orders/sink2/*`).
- T0 quiet.

**Workload.** W-1 + W-2 on Hub continuous from T0 through endpoint, ~50 ops/min total, partitioned across prefixes:
- W-1 doc CRUD: ~40% `users/{i}`, ~40% `orders/{i}`, ~20% `internal/{i}`
- W-2 extension ops: drawn proportionally across the three prefixes from the seeded pool

**Variants — same topology, same workload, same RNG seed; only the upgrade order differs.**

| Variant | Upgrade order | Cross-version surface uniquely exercised |
|---|---|---|
| **A — sinks first** | sink-1 (3 nodes) → hub (3 nodes) → sink-2 (3 nodes) | `v_62 sender → v_new receiver` at Checkpoint A (hub `v_62` → sink-1 `v_new`); partial new-lane activation at Checkpoint B |
| **B — hub first** | hub (3 nodes) → sink-1 (3 nodes) → sink-2 (3 nodes) | `v_new sender → v_62 receivers` on both sink legs at Checkpoint A |
| **C — interleaved** | seeded random shuffle of all 9 nodes, one at a time | intra-cluster mixed-binary states dwelt-in across all 3 clusters concurrently (most realistic) |

**Scenario Recipe (per variant).**

| # | Phase | Step |
|---|---|---|
| 1 | setup | Provision T3 all-`v_62`; create database with the plan's per-collection revisions config (`Users` / `Orders` / `Internal`); wire filtered pull (sink-1 filter `users/sink1/* + orders/sink1/*`; sink-2 filter `users/sink2/* + orders/sink2/*`) |
| 2 | setup | Seed Hub with the 7 prefix buckets in v_62 raw-CV form (~2k per bucket for users/orders, 3k for Internal); wait for filtered drain — sink-1 receives only its bucket, sink-2 receives only its bucket, hub-only buckets stay on hub |
| 3 | T0 | **T0 baseline checkpoint** — full validator suite (see below) on the all-`v_62` cluster |
| 4 | workload | Start W-1 + W-2 (partitioned across the 3 prefixes); continuous through the rest of the scenario |
| 5 | upgrade #1 | Execute the variant's first upgrade step (cluster-at-a-time for A/B, single node for C) |
| 6 | checkpoint A | Wait for drain; run **Checkpoint A** (lane invariants + filter invariants — see below) |
| 7 | upgrade #2 | Execute the variant's second upgrade step |
| 8 | checkpoint B | Wait for drain; run **Checkpoint B** |
| 9 | upgrade #3 | Execute the final upgrade step → all 9 nodes `v_new` |
| 10 | endpoint | Wait for drain; run **endpoint checkpoint** — new lane active on every connection |
| 11 | finish | Stop W-1 + W-2; full final validator suite; teardown |

**Variant C checkpoint cadence.** Same Checkpoint A / B / endpoint pattern, but the "upgrade step" between checkpoints is 3 random-order nodes instead of a whole cluster. Full RNG seed captured for replay.

**Failure Injection.** None — the rolling upgrade and mid-roll permutations are the test surface.

**Per-checkpoint validator suite.**

| Family | Checks |
|---|---|
| **Lane invariants** | `assert_old_lane_inert_on_v_old_peer` on every v_new node connected to a v_62 peer — no new-lane artifacts persist on cross-version connections. At endpoint, I-13 on every connection. |
| **Filter invariants** | I-7 across all 7 buckets: (i) sink-1 has only `users/sink1/*` + `orders/sink1/*`; (ii) sink-2 has only `users/sink2/*` + `orders/sink2/*`; (iii) `*/hub/*` and `Internal/*` stay on hub; (iv) every sink-routed doc on hub appears on the matching sink (per-prefix count parity); (v) no leak across sinks |
| **Convergence** | I-1 doc count parity per prefix bucket; I-2, I-3 (proxy), I-5, I-6 |
| **Schema chain** | I-10 — every upgraded node ran the full `62000 → ... → 72001` chain to completion |

**Expected Invariants.**

| Checkpoint | Anchor invariants |
|---|---|
| T0 baseline | I-1, I-2, I-5 on all-`v_62` topology; filter geometry holds at the start |
| Mid-roll (any cross-version peer present) | Lane invariants + filter invariants + convergence; I-10 mixed during, converged after each upgrade step |
| Endpoint (all v_new) | I-1..I-7 + I-11 + I-13 on every connection + I-10 (all 72001) + full filter geometry |

**Validation Strategy.** Each upgrade step requires a drain before the validator runs so checkpoint state is steady, not transient. Lane-inert validator runs at every mid-roll checkpoint. Filter invariants run at every checkpoint including T0 (catches any seeding leak). Endpoint runs the full I-13 suite — confirms the new lane activates only once the cluster has fully converged. Variant C captures the random-shuffle sequence for forensic replay.

**Expected Failure Modes.**
- v_new node fails to detect a v_62 peer mid-roll → new-lane artifacts appear before endpoint (lane-inert fails).
- Filter geometry breaks under cross-version replication: a sink-2 bucket leaks to sink-1, or a sink-1 bucket is skipped on sink-1 (filter invariants fail).
- Endpoint fails to flip to new lane after all nodes v_new → I-13 fail despite all peers being v_new.
- Schema chain `62000 → ... → 72001` doesn't complete on a node → I-10 mismatch.
- Variant C: intra-cluster mid-roll causes split-CV writes on a v_new node that still has a v_62 peer in its own cluster.

**Execution Notes.** Each variant ~30 min wall (3 upgrade steps × ~5 min + 4 checkpoints + W-1/W-2 continuous + final convergence). 3 variants sequential ≈ 90 min total; in parallel (if hardware allows 3 × 9 containers) ≈ 30 min. Phase C.

---

<a id="rpv-2"></a>
### RPV-2 — Mixed-form replication + tombstone cleanup + backup chain + restore

**Purpose.**
- Single Class 2 mega-scenario covering the entire backup / restore + cross-form replication lifecycle.
- **The scenario runs in two rounds** — round 1 is the clean lifecycle; round 2 repeats the same lifecycle with a sink-leader failover injected during phase (a) (active cross-form replication).
- Hard-killing the sink-leader is the round-2 fault: from the rest of the cluster's perspective it is equivalent to a partition of that node until sink-1 elects a new leader.
- Round 2 proves the same chained operations converge to the same correct state when the sink-leader is lost mid-flight.

1. Mixed-form revisions replicate correctly across all peers (cross-form wire correctness).
2. Tombstone cleanup converges across mixed forms (revision purge under aggressive config).
3. Snapshot backup preserves PK forms across restore.
4. Smuggler backup preserves filter spec on restore.
5. Backup doesn't stall active replication.
6. Restored database resumes replication correctly to a fresh sink.
7. The whole lifecycle survives a sink-leader hard-kill during active replication and converges to the same final state as the clean baseline.

**Topology.** Class 2 — T2 source topology with **sink-1 RF = 3** so sink-leader failover is possible + two fresh restore-target clusters per round (one pair for round 1, a new pair for round 2). All v_new.

**Initial State.**
- Default revisions config (the `Users` collection's keep-10 / max-age-30-min is tight enough for purge to fire mid-scenario as W-1 + W-2 + W-3 hammer the `users/sink1/*` pool).
- Hub seeded with 15k raw-CV + 15k hashed = 30k revisions in `users/sink1/*` + `users/hub/*` + `Internal/*` buckets (mixed-form via the *Legacy-form revision seed* recipe), plus attachments + counters + time-series on a 1k-doc subset.
- Filter `users/sink1/*` on sink-1.

**Workload.**
- W-1 + W-2 on Hub for ~10 min per round — W-1 generates ~15k doc ops, W-2 generates ~10k attachment + counter + TS operations.
- Delete-storm bursts (W-3) interleaved every 90 s drive revision tombstone creation.
- The source-side workload trace is captured per round so round 2's expected post-state can be compared cleanly against round 1's.

**Scenario Recipe.**

The scenario runs the same phase (a)–(e) lifecycle twice, sequentially. Round 1 is the clean baseline. Round 2 repeats with a sink-leader hard-kill (failover) injected mid-phase (a). The source topology stays alive across rounds (round 2 extends the existing dataset). Restore-target clusters are fresh per round so the form-sampling checks aren't polluted by round-1 state.

**Round 1 — clean lifecycle**

| # | Phase | Step |
|---|---|---|
| 1 | setup | Provision source T2 (sink-1 RF = 3, all v_new) + two empty restore-target clusters for round 1; create databases with the plan's default revisions config; configure `users/sink1/*` filter on sink-1 |
| 2 | setup | Seed Hub mixed-form (15k raw-CV + 15k hashed via the *Legacy-form revision seed* recipe); add attachments + counters + TS on the 1k-doc subset; wait for filtered replication to drain to both sinks |
| 3 | **(a)** cross-form replication | Start W-1 + W-2 on Hub for 5 min — generates new revisions in mixed-form pattern. Continuously compare change vectors across all 3 peers every 30 s |
| 4 | (a) | Phase-(a) checkpoint: I-1, I-2 (cross-form CV equality), I-3 (proxy), I-4 (adopt probe). I-2 PK form match not enforced cross-cluster (sinks store in supported form) |
| 5 | **(b)** tombstone cleanup mixed forms | W-3 delete-storm burst for 3 min on Hub (intercut with W-1 + W-2). Purge fires (Users-collection keep-10 + 30-min max age). Snapshot tombstone count pre and post purge; sample a few doc IDs via `/revisions?id=` (checking field-12 presence) to confirm both forms are being cleaned |
| 6 | (b) | Phase-(b) checkpoint: I-2 strict tombstone parity hub vs sink-1; both raw-CV and hashed-form cleanups converge |
| 7 | **(c)** backup during replication | Continue W-1 + W-2. Trigger a Snapshot backup of Hub. Continue W-1 + W-2 throughout |
| 8 | (c) | Wait for snapshot completion. Observe Hub's `LastSentEtag` progresses throughout the backup window (I-6 spot check) |
| 9 | (c) | Trigger a Smuggler backup of Hub to a separate destination. Continue W-1 + W-2. Wait for completion |
| 10 | (c) | Phase-(c) checkpoint: I-6 — neither backup stalled replication |
| 11 | **(d)** restore + form preservation | Stop W-1 + W-2 on source; quiet. Restore the snapshot to the round-1 snapshot-target. Sample a deterministic set of seeded doc IDs and verify via `/revisions?id=` that field-12 presence on the snapshot-target matches the source (snapshot is byte-identical at voron page level) |
| 12 | (d) | Restore the smuggler dump to the round-1 smuggler-target. Sample the same doc IDs and verify field-12 is present on every returned revision (smuggler re-writes via public API → all revisions end up hashed) |
| 13 | (d) | Verify the filter spec (`users/sink1/*` on sink-1) was preserved on the smuggler-restored database |
| 14 | (d) | Phase-(d) checkpoint: form sampling matches expectation per backup type; I-7 filter preservation |
| 15 | **(e)** post-restore replication | Configure pull replication from the round-1 snapshot-target to a fresh sink. Run W-1 against the snapshot-target for 5 min. Wait for drain |
| 16 | (e) | Phase-(e) checkpoint: restored sink converges with snapshot-target; restored database can serve as replication source |
| 17 | round-1 finish | Round-1 final validation: I-1, I-2, I-3, I-4 across all source + round-1 restored peers. Snapshot the source-side workload trace + state for round-2 comparison. |

**Round 2 — same lifecycle with sink-leader failover during phase (a)**

| # | Phase | Step |
|---|---|---|
| 18 | reset | Tear down the round-1 restore-target clusters (snapshot + smuggler). Provision two fresh empty restore-target clusters for round 2. Source topology stays alive (round 2 extends the dataset). |
| 19 | **(a)** cross-form replication | Restart W-1 + W-2 on Hub. Replicate to sink-1 (current leader `2a`) and sink-2 normally for 90 s. |
| 20 | (a) **failover** | At T+90 s into phase (a), hard-kill the sink-1 leader (`2a`) via `kill_ravendb_hard`. From Hub's perspective `2a` is now unreachable — equivalent to a single-node partition until `2a` comes back. sink-1 elects a new leader from `{2b, 2c}`. Hub reconnects to the new sink-1 leader and resumes sending. Continue W-1 + W-2 for 3 more minutes after the new leader is elected. |
| 21 | (a) | Phase-(a) round-2 checkpoint: I-1, I-2, I-3 (proxy) across Hub + sink-1 (new leader) + sink-2 post-failover. sink-1's new leader holds the pre-failover state plus what was sent after election. I-6 no stuck replication task. |
| 22 | (a) → (b) | Restart `2a`. Wait for it to rejoin sink-1 as a full member. Brief quiet so the rejoin doesn't race with phase (b). |
| 23 | **(b)** tombstone cleanup mixed forms | Same as round-1 step 5 — W-3 delete-storm burst on Hub, aggressive purge fires, sample doc IDs for both forms |
| 24 | (b) | Phase-(b) round-2 checkpoint: I-2 strict tombstone parity Hub vs sink-1 (all 3 nodes, including the rejoined `2a`); cleanup converges on both forms |
| 25 | **(c)** backup during replication | Same as round-1 steps 7-9 — Snapshot backup, then Smuggler backup, while W-1 + W-2 continues |
| 26 | (c) | Phase-(c) round-2 checkpoint: I-6 — neither backup stalled replication on the recovered topology |
| 27 | **(d)** restore + form preservation | Same as round-1 steps 11-13 — restore both backups to the fresh round-2 restore-target clusters; sample form-distribution |
| 28 | (d) | Phase-(d) round-2 checkpoint: form sampling matches expectation per backup type; I-7 filter preservation. The data backed up in round 2 includes the post-failover recovery state — form sampling validates that the recovery state was preserved across the backup chain. |
| 29 | **(e)** post-restore replication | Same as round-1 step 15 — wire pull replication from round-2 snapshot-target to a fresh sink; W-1 burst |
| 30 | (e) | Phase-(e) round-2 checkpoint: restored sink converges |
| 31 | round-2 finish | Round-2 final validation: I-1, I-2, I-3, I-4 across all source + round-2 restored peers. |
| 32 | cross-round compare | Compare round-1 and round-2 final states on the per-doc invariants of the round-1 doc set: same lifecycle of operations must produce the same convergent end-state. Round-2 dataset is a superset (extended workload) — comparison is restricted to per-doc I-2, I-3 (proxy) on the round-1 doc IDs. |

**Failure Injection.**
- Round 1: none — the chained operations are the test. Lateral chaos comes from running two backup types over a continuous doc + extension workload while replication is active.
- Round 2: a sink-1 leader hard-kill at T+90 s into phase (a). From Hub's perspective this is equivalent to a partition of `2a` until it rejoins. sink-1 elects a new leader from `{2b, 2c}` and replication resumes. `2a` rejoins between phase (a) and phase (b).

**Expected Invariants.**

| Round / Phase | Invariants |
|---|---|
| R1 (a) | I-1, I-2 (cross-form CV equality), I-3 (proxy), I-4, I-5, I-6 |
| R1 (b) | I-2 strict tombstone parity Hub vs sink-1; cleanup of both raw-CV and hashed composites |
| R1 (c) | I-6 — no stall during either backup |
| R1 (d) | Form sampling: snapshot-target preserves source form; smuggler-target = all hashed. I-7 filter preserved on smuggler restore. I-2 equality across source and restored target. |
| R1 (e) | I-1, I-2, I-3, I-4, I-5, I-6 between restored source and its new sink |
| R1 finish | I-1, I-2, I-3, I-4 across all source + round-1 restored peers |
| R2 (a) post-failover | I-1, I-2, I-3 (proxy) across Hub + sink-1 (new leader) + sink-2 within budget after the new leader is elected. I-6 no stuck task. |
| R2 (a) → (b) | `2a` rejoins sink-1 as a full member; I-1, I-2 across all 3 sink-1 replicas after rejoin |
| R2 (b)..(e) | Same invariants as the round-1 counterparts, applied to the recovered topology and the round-2 restore-target clusters |
| R2 finish | I-1, I-2, I-3, I-4 across all source + round-2 restored peers |
| cross-round | Per-doc invariants (I-2, I-3 proxy) on the round-1 doc set must agree between round-1 and round-2 final states |

**Validation Strategy.** Per-phase assertions per the table. `voron_growth_envelope` (whole-DB `/stats.SizeOnDisk`) snapshot pre / post each backup confirms the backup window did not introduce egregious bloat. The "did not lose mixed-form rows" check is a sparse-sample composition (workload trace ↔ `/revisions?id=`) pre / post backup. Cross-round compare uses `PC-4` (aggregate count parity) + `PC-2` (per-doc CV multiset on a sampled probe set) on the round-1 doc-ID set to confirm the failover-stressed lifecycle converges to the same per-doc state as the clean baseline.

**Expected Failure Modes.**
- Backup stalls replication (acquires a write lock that blocks the sender) → I-6 stuck during phase (c) of either round.
- Snapshot restore loses raw-CV rows (field-12 absence mis-handled on restore) → form sampling at (d) catches a missing raw-CV row.
- Smuggler restore loses the filter spec → I-7 fails at (d).
- Cross-form replication fails on a sink that stores in a form different from the source's → I-2 mismatch at phase (a) of round 1.
- Tombstone cleanup operates only on hashed form, leaving raw-CV orphans → tombstone parity fails (I-1 + I-2) at phase (b) of round 1.
- Round 2 failover: replication fails to resume on the new sink-1 leader after the kill → I-6 stuck at the post-failover checkpoint. Or the new leader has lost in-flight replication state from `2a` → I-2 mismatch between Hub and sink-1 (new leader). Or `2a` rejoin races with phase (b) tombstone cleanup → I-2 tombstone parity diverges across the 3 sink-1 replicas after rejoin.
- Round 2 cross-round compare: round-2 final per-doc state diverges from round-1 final on the shared doc set → the failover-stressed lifecycle didn't converge to the same end-state as the clean baseline (silent corruption induced by sink-leader loss).

**Execution Notes.** Wall ~60 min for two rounds × phases (a)–(e) (round 1 ~30 min + round 2 ~30 min with failover + rejoin added). Phase C.

---

## Class 3 — Sharded ↔ Non-Sharded

Sharded pull / push replication is not supported. Cross-topology data movement uses smuggler, ETL, or backup / restore. Sharded internal replication (between replicas of the same shard) is exercised here.

<a id="rv-2"></a>
### RV-2 — Sharded minimum (internal rolling upgrade + sharded → non-sharded smuggler)

**Purpose.** Minimum sharded coverage — must run before every release. Two surfaces:
1. Sharded internal replication moves revisions between shard replicas correctly during a rolling upgrade.
2. Sharded → non-sharded smuggler export preserves revision PK form via public-API re-write.

The extended sharded coverage (reverse-direction smuggler import + ETL / backup 3-way cross-check) lives in **RV-3**.

**Topology.** Class 3, T4 — sharded source cluster (3 shards × 3 replicas = 9 nodes) + one non-sharded smuggler target.

**Initial State.**
- Default revisions config on all clusters.
- Sharded source on `v_62`: 30k docs sharded across `Sx` / `Sy` / `Sz` (10k per shard) × 3 revs = 90k revs total in v_62 raw-CV form.
- Non-sharded target empty.

**Workload.**
- W-1 during the rolling upgrade phase (a).
- W-9 (cross-topology drain — smuggler export → import) is the phase (b) data-movement step.

**Scenario Recipe.**

| # | Phase | Step |
|---|---|---|
| 1 | setup | Provision sharded source on v_62 + non-sharded smuggler target; seed sharded source on v_62 |
| 2 | **(a)** internal rolling upgrade | Start W-1 distributed across shards |
| 3 | (a) | Roll-upgrade `Sx` (3 replicas one at a time); checkpoint after each shard finishes |
| 4 | (a) | Roll-upgrade `Sy`; checkpoint |
| 5 | (a) | Roll-upgrade `Sz`; checkpoint |
| 6 | (a) | Stop W-1; quiet; phase-(a) final: per-shard I-1, I-2, I-3, I-4, I-5, I-6, I-10 |
| 7 | **(b)** sharded → non-sharded smuggler | Trigger smuggler backup on sharded source to a destination |
| 8 | (b) | Restore the dump into the non-sharded target |
| 9 | (b) | Phase-(b) checkpoint: I-1, I-2, I-3, I-4 between source (union across shards) and non-sharded target; PK form on target = all hashed (smuggler re-writes via public API) |

**Failure Injection.** Rolling upgrade in phase (a); no chaos in phase (b).

**Expected Invariants.**

| Phase | Invariants |
|---|---|
| (a) | Per-shard I-1, I-2, I-3, I-4, I-5, I-6, I-10 (rolling upgrade convergence) |
| (b) | Union of shards = non-sharded target; PK form on target all hashed |

**Validation Strategy.** Per-phase checkpoint blocks the next phase.

**Expected Failure Modes.**
- Phase (a): rolling upgrade of one shard's replicas breaks per-shard consistency → caught at per-shard checkpoint.
- Phase (b): smuggler export from a sharded source loses revisions on a specific shard → I-2 mismatch.

**Execution Notes.** Wall ~20 min. Phase C (runs first in C — quick sharded migration check).

---

<a id="rv-3"></a>
### RV-3 — Sharded extended (reverse-direction import + ETL / backup 3-way cross-check)

> **TODO — on hold pending Karmel re-spec.** Topology + phase scope have open questions (target clusters / ETL / backup-compare may collapse). Scenario directory renamed to `RV3-TODO/` and dropped from `scripts/run_all_overnight.sh`. The section below reflects the OLD spec; do not implement against it until Karmel confirms the new shape.

**Purpose.** Extended sharded coverage on top of RV-2. Two additional surfaces:
1. Non-sharded → sharded smuggler import routes every doc to the correct shard.
2. ETL + parallel backup from a sharded source enables a 3-way cross-check (ETL target == backup-restored target == sharded source union).

**Topology.** Class 3, T4 — sharded source cluster (3 shards × 3 replicas) + one fresh non-sharded source + three non-sharded target clusters (sharded-import target + ETL target + backup-restore comparison target).

**Initial State.**
- Default revisions config on all clusters.
- A fresh non-sharded source seeded with 20k docs × 3 revs = 60k revs mixed-form.
- Sharded source from RV-2's end state OR re-seeded as needed.

**Workload.**
- W-1 on the sharded source during phase (b).
- W-9 (cross-topology drain — smuggler export/import and ETL) is the data-movement step across both phases.

**Scenario Recipe.**

| # | Phase | Step |
|---|---|---|
| 1 | setup | Provision a fresh non-sharded source + sharded-import target + two more non-sharded targets (ETL + backup comparison) |
| 2 | **(a)** non-sharded → sharded smuggler (reverse) | Seed the non-sharded source with 20k docs × 3 revs = 60k revs mixed-form |
| 3 | (a) | Trigger smuggler backup on the non-sharded source |
| 4 | (a) | Restore the dump into the fresh sharded target. Verify orchestrator routes every doc to the expected shard given the docId hash |
| 5 | (a) | Phase-(a) checkpoint: I-1 (target doc count = source doc count), I-2 (per-doc revision count), I-2 (per-doc CV equality between source and the shard that owns it), PK form check (target: all hashed via smuggler), I-5, I-6 |
| 6 | **(b)** ETL + backup cross-check | Re-seed the sharded source with 20k × 3 revs (mixed-form). Configure ETL from sharded → a separate non-sharded target |
| 7 | (b) | Trigger a smuggler backup on the sharded source to a second destination |
| 8 | (b) | Run W-1 on the sharded source for 5 min; observe ETL drain |
| 9 | (b) | Stop W-1; quiet. Restore the backup to a third non-sharded comparison target |
| 10 | (b) | Phase-(b) checkpoint: ETL target == backup-restored target == sharded source union (I-2 union equality across all three) |

**Failure Injection.** None — data movement is the test.

**Expected Invariants.**

| Phase | Invariants |
|---|---|
| (a) | I-1 doc count, I-2 per-doc revs, I-2 CV equality source vs owning shard, PK form check: target all hashed |
| (b) | I-2 union equality across ETL target, backup-restored target, and sharded source |

**Validation Strategy.** Phase (b)'s 3-way cross-check is the load-bearing assertion — divergence between any two of the three targets fails the run.

**Expected Failure Modes.**
- Phase (a): orchestrator routing on import places docs on the wrong shard → I-2 (CV) mismatch when comparing source vs owning shard.
- Phase (b): ETL drain race with the workload produces divergence vs the backup snapshot → 3-way cross-check fails.

**Execution Notes.** Wall ~25 min. Phase C (runs after RV-2). Budget time to reconstruct the source set if a divergence is detected.
