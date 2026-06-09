# company-1 Testing Plan — Context

The foundational definitions every other file refers to: scenario taxonomy, version handles, testing philosophy, workloads, revisions configuration, invariants, risks, and false-positive mitigation.

## Testing philosophy

1. **Convergence is the contract.** Every scenario reaches a quiescent state and asserts byte-equality of revision state across peers — counts, change vectors, and consolidated per-doc stats.
2. **Failure is the workload.** Partitions, restarts, mentor swaps, and filter mutations are interleaved with reads and writes by design.
3. **Mixed-form is the default.** Raw-CV and hashed-form revisions coexist; tests seed both, replicate both, and verify identity preservation.
4. **Pass / fail is mechanical.** Validators output `OK | LAG | STUCK | CORRUPT`. Only `STUCK` and `CORRUPT` fail; `LAG` extends the budget once.
5. **Two-budget waits, no static sleeps.** Every wait has a cluster-health budget and a replication-quiet budget; both close before validation.
6. **Deterministic seeds.** Every workload uses deterministic IDs and seeded RNG. A failing soak run is replayable from its captured seed.
7. **Operational-language recipes.** Recipes read in plain English; the orchestration layer in [primitives.md](primitives.md) and the proposed validators in [new_primitives.md](new_primitives.md) wire the calls.

## Scenario taxonomy

Scenario IDs encode which fix surface a test exercises.

| Prefix | Meaning |
|---|---|
| **RV** | Revisions — exercises the revision PK migration (not the filtered-replication fix) |
| **RP** | Replication-only — exercises the filtered-replication fix |
| **RPV** | Both — exercises revisions migration and filtered replication together |
| **SK** | Soak — 2 h longevity scenario |

## Version handles

| Handle | Meaning |
|---|---|
| `v_62` | Last v6.2 release (schema 62000) — the source binary for every upgrade scenario |
| `v_new` | This branch (schema 72001, hashed PK, CV-boundary + cursor fixes) |

A `v_62 → v_new` upgrade traverses the full schema chain `62000 → ... → 72001` internally as a sequence of intermediate steps; tests validate the chain runs to completion and a restart mid-chain resumes safely. Mixed `v_62 ↔ v_new` nodes can coexist inside the same cluster and across replicated clusters — both are real configurations the engine must tolerate.

## Topology IDs

A topology ID names only the **physical provisioning shape** — how many clusters and nodes to spin up. Filter direction (pull / push / bidirectional), allowed-paths, replication factor, and which nodes start on `v_62` vs `v_new` are per-scenario configuration, applied by a separate wiring step and described inline by each scenario. IDs are ordered by cluster count.

| ID | Shape | Clusters | Used by |
|---|---|---|---|
| **T1** | Single cluster, 3 nodes | 1 | RV-1 |
| **T2** | Hub + 1 sink  (3 nodes each)| 2 | RP-1, RP-3, RPV-2 (+ restore-target clusters) |
| **T3** | Hub + 2 sinks  (3 nodes each) | 3 | RP-2 (bidirectional + overlapping), RPV-1 (disjoint 7-bucket), SK-1 (filtered + unfiltered), SK-2 (disjoint 7-bucket) |
| **T4** | Sharded 3×3, + non-sharded targets (count per scenario) | 1 sharded + 1 regular | RV-2, RV-3 |

## Workloads

Each scenario declares one or more workloads by ID. Workloads describe **what operations they perform** — they do not list which entities those operations produce. Workloads compose: a scenario can run `W-1 + W-2` to get both doc churn and extension churn on the same database.

| ID | Description | Op grammar | Concurrency | Scale |
|---|---|---|---|---|
| W-0 | One-shot deterministic seed | Bulk put with deterministic IDs | 1 writer | 10k–100k docs |
| W-1 | Doc CRUD churn | 70% update / 20% put / 10% delete on the seeded doc pool | 6 writers / cluster | 10–30k ops |
| W-2 | Doc-extension churn | 25% attachment-add / 25% attachment-remove / 12.5% counter-inc / 12.5% counter-dec / 12.5% TS-append / 12.5% TS-delete on docs already in the pool. No doc CRUD. | 4 writers / cluster | 10–20k ops |
| W-3 | Delete-storm | 80% delete / 20% put (puts replenish the pool for sustained delete pressure) | 4 writers / cluster | 10–20k ops |
| W-4 | Filter-boundary churn | 50% writes on filter-in IDs / 50% on filter-out IDs | 6 writers / cluster | 10–20k ops |
| W-5 | Conflict generator | Two writers on the same doc-id pool with distinct content; run under split-brain partition | 2 × 2 (one per partitioned side) | 10k conflicted ops |
| W-6 | Long-lived soak baseline | W-1 grammar at lower rate sustained over the 2 h soak window | 2 writers / cluster | ~10–20k over 2 h |
| W-7 | Large-history single doc | Append revisions to `users/hot` indefinitely | 1 writer | 1M–5M revisions |
| W-8 | Revisions-subscription consumer *(deferred — subscriptions out of scope this PR)* | Open a revisions-subscription, ack with jitter | 1 consumer / sink | passive |
| W-9 | Cross-topology drain | Export source to file → import to target via smuggler / ETL | n/a | 10–50k revisions moved |

Write locality: each workload names the cluster explicitly. For hub-sink filtered scenarios, default is hub-writes-only unless the scenario specifies otherwise.

**Filter-aware workloads.** When a scenario uses T3 with the disjoint 7-bucket wiring, W-1 and W-2 partition their op stream across the 7 ID buckets defined in the *Revisions configuration* section so each sink receives a known subset, hub-only docs stay on the hub, and every per-checkpoint validator can verify filter geometry by exact prefix.

## Revisions configuration

The plan uses **one canonical revisions configuration** for every database. The config is intentionally *not* flat — it defines a different purge regime per collection so every scenario that writes across multiple collections exercises multiple purge code paths in parallel:

| Collection | Keep latest | Max age | Profile |
|---|---|---|---|
| `Users` | 10 | 30 min | Tight — purge fires fast under churn; lots of tombstones at high write rate |
| `Orders` | 50 | 2 hours | Moderate — typical real-world history depth |
| `Internal` | 25 | 6 hours | Loose — used by the hub-only collection in filter-aware scenarios |
| Default (any other collection) | 25 | 6 hours | Same as `Internal` — long-running scenarios stay bounded |

The configuration is set as part of database creation. Recipes never include a "change revisions config" step.

**Overrides.** One scenario overrides the per-collection rules with a globally **keep-all** configuration:

| Scenario | Override | Why |
|---|---|---|
| RV-1 phase 3 | Keep-all (no purge on any collection) | Phase 3 accumulates a 1M-revision history on a single doc — needs unbounded retention |

RPV-2 (the tombstone-cleanup test) doesn't need an override: writes to `users/sink1/*` naturally trigger purge once each doc passes the `Users`-collection keep-10 / 30-min thresholds.


## Doc-ID conventions for filter routing. 

Filter-aware scenarios use sub-prefixes within `Users` and `Orders` collections to route docs to specific sinks. The revisions config is uniform per collection, so all `Users` docs share one purge regime regardless of which sink they land on.

| Prefix | Collection | Routes to |
|---|---|---|
| `users/sink1/{i}` | `Users` | sink-1 |
| `users/sink2/{i}` | `Users` | sink-2 |
| `users/hub/{i}` | `Users` | hub only (no sink filter matches) |
| `orders/sink1/{i}` | `Orders` | sink-1 |
| `orders/sink2/{i}` | `Orders` | sink-2 |
| `orders/hub/{i}` | `Orders` | hub only |
| any doc in `Internal` | `Internal` | hub only (by collection — no sink filter matches `Internal/*`) |

This gives every filter-aware scenario three collections × three destinations + one hub-only collection — a 7-bucket workload partition. Per-checkpoint assertions verify each bucket landed where the filter spec says it should (no leak, no skip) and that the per-collection purge regime fired correctly across all destinations.

## Invariants

Each invariant is one assertion the plan stands behind. Right column is the validator primitive ID (full spec in [primitives.md](primitives.md) / [new_primitives.md](new_primitives.md)).

### Convergence — peer-to-peer parity

| ID | What it asserts | Validator |
|---|---|---|
| **I-1** | **Identical entities counts across peers** — same count of documents, revisions per doc, tombstones per type, attachments, counters, time-series segments on every peer | PC-4 |
| **I-2** | **Per-doc metadata equality across peers** — for every doc, all peers have the same change vector, the same revision rows by `(docId, CV.Version)`, the same attachment hashes, the same counter values, and the same time-series segment hashes | PC-2 (CV multiset on sampled probe set) + PC-4 (aggregate counts) |

### Workload-trace consistency

| ID | What it asserts | Validator |
|---|---|---|
| I-3 | **What's stored matches what was written** — for every doc the workload touched, the local revision count matches the expected count from the captured workload trace. Indirect duplicate-detection: an extra row shows up as count drift. | composition over the workload trace |
| I-4 | No orphan revisions — adopt-path probe yields zero adoptions on any peer | PC-3 |

### Operational health

| ID | What it asserts | Validator |
|---|---|---|
| I-5 | **Marker-doc propagation** — a uniquely-IDed sentinel doc written on the source appears on every replication destination within budget. End-to-end "replication is flowing" check (complements `wait_for_quiescence` which checks for a steady state). | PC-12 |
| I-6 | No replication task in `Faulted` / `Error` state; no reconnect loop persists | PD-1 |
| I-7 | Filter compliance — every doc on a sink matches the filter; no source doc matching the filter is missing on the sink | PC-5 + PC-8 |
| I-8 | Storage envelope — whole-DB `SizeOnDisk` within bounds (± 50% by default) | PC-6 |
| I-9 | No silent backlog growth in soak — max backlog stays below the ceiling and drains within the quiet budget over any 30 min window | PC-7 |
| I-10 | Schema-version convergence at quiescence — all peers report the same `database.schema_version`; mixed schemas only allowed during a rolling upgrade window | PC-1 |

### Failover & conflict

| ID | What it asserts | Validator |
|---|---|---|
| I-11 | Post-heal conflict resolution converges — all peers resolve every previously-conflicted doc to the same chain via the configured resolver | PC-2 + `/replication/conflicts` empty check |
| I-12 | **Replication survives sender / receiver failover** — after a crash on either side, post-recovery I-1 + I-2 still hold; no duplicates, no missing items; the failover cursor advances correctly. (Consolidates the earlier cursor-protocol sub-observables into one; PD-11 and PD-12 stay available as forensic primitives when a failure needs root-causing.) | post-recovery I-1 + I-2 + I-6; forensic detail via PD-11 / PD-12 |

### CV-boundary fix surface

| ID | What it asserts | Validator |
|---|---|---|
| I-13 | **CV-boundary correctness on a v_new receiver** — after filtered ingress: **(a)** every stored item CV is split-shaped for the new lane; **(b)** the receiver's `LastDatabaseChangeVector` references only receiver-group nodes — no source-side leak (**the load-bearing regression guard**); **(c)** internal replicas in the receiver group preserve the stored item CV; **(d)** conflict detection / echo prevention compares on the source-side identity. | PC-9 (item-CV split — single node and across the receiver group) + PC-10 (DB-CV order-side) |

**Lane behavior** (referenced by RV-1, RPV-1, RV-2, SK-2). The new lane on a connection is active only when **both** peers are `v_new`. Any `v_62` peer in a connection forces both peers onto the old lane for that connection. Lane is determined by binary version — there is no runtime toggle. The lane-inert validator (`assert_old_lane_inert_on_v_old_peer`) confirms a v_new node connected to a v_62 peer leaves no new-lane artifacts (no split-shape item CVs, no durable cursor records).

## Risk analysis

Risks grouped by what they touch. Each row gives the bug class and why this branch raises it; the **risk × scenario matrix** at the end of this section maps every risk to the scenarios that target it. (Rachis / Raft consensus is **unchanged by this PR** — leader election, quorum, and membership mechanics are out of scope; cluster faults appear only as chaos that stresses the in-scope storage and replication layers.)

### Revisions migration risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-01 | Silent revision divergence between peers running different binaries — a `v_new` receiver must accept and store the raw-CV form a `v_62` peer sends, and a `v_new` sender must down-convert to raw-CV when it replicates to a `v_62` peer | A cross-version connection runs the **old lane**: the `v_new` sender emits raw-CV (not its native hashed form), and the `v_new` receiver stores incoming raw-CV as a legacy row. A bug in the down-convert or the accept path silently diverges the two peers |
| R-02 | Hash collision masks distinct revisions | New 22B hash digest of `CV.Version` |
| R-03 | Legacy raw-CV revision rows not migrated to the hashed PK | After upgrade, a document's existing `v_62` raw-CV revision rows must be migrated to the new hashed PK; any path that leaves a legacy row in place while the hashed form also exists yields duplicate revisions |
| R-04 | Duplicate revisions after retry storms | Pull replication retries with the same source etag could double-insert if PK form differs |
| R-05 | Unbounded revision growth under churn | Hashed PK is fixed-width — pathological CVs could pin the etag prefix into a hot range |
| R-06 | Per-doc stat desync (attachments / counters / TS) | Revision PK migration could decouple a revision row from its attachment / counter / TS stream |

### Upgrade / schema-chain risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-07 | `v_62 → v_new` upgrade traverses the full schema chain | Chain runs `62000 → ... → 72001` on the upgraded node; restart mid-chain must resume safely |
| R-08 | Mixed-version cluster starts using the new lane prematurely, or fails to switch when ready | Lane is binary-version-determined; mixed clusters must stay on the old lane until fully converged |

### Filtered-replication risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-09 | Filtered ingress can silently skip items, advance cursors wrongly, or merge conflicts incorrectly (CV-boundary fix) | Pre-fix path was unsafe; v_new fixes it |
| R-10 | Failover loses replication position (cursor fix) | v_new uses a durable Sink-owned failover cursor |
| R-11 | Conflict resolution divergence | Two conflicting writes resolved differently on different nodes → different post-resolution revision chain |
| R-12 | Subscription drift during failover | Subscription resume etag tied to revision state. **Deferred this PR** — subscriptions out of scope; the `open_subscription` stub fails loudly. |

### Replication operational risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-13 | Mentor reassignment loses backlog | Hub / sink mentor flip mid-replication could orphan a batch |
| R-14 | Stuck replication state (queue never drains) | Filter mutation + reconnect could leave dangling `LastEtag` pointers |
| R-15 | Membership change (add / remove / re-bootstrap a node or sink) during migration or active replication | Add / remove a node, or wipe + rejoin a whole sink, while rows are mixed-form and/or replication is streaming — backlog and migration state must survive (the membership mechanics themselves are Raft and out of scope; only the data outcome is asserted) |

### Backup & restore risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-16 | Backup / restore loses lazy-migration state | Snapshot restore (byte-identical) vs smuggler restore (re-writes all via public API) handle PK forms differently |
| R-17 | Cross-version / mid-migration restore | Restoring a `v_62`-era or half-migrated (mixed-form) backup into a `v_new` cluster must migrate and serve correctly |
| R-18 | Restored sink resumes replication from the correct position | After restore, a sink must re-establish its durable failover cursor + filter spec and resume without re-sending or skipping |

### Sharding risks

| # | Risk | Why this branch raises it |
|---|---|---|
| R-19 | Sharded internal replication of revisions under chaos | Sharded pull / push not supported, but sharded *internal* replication still moves revisions across replicas of the same shard |
| R-20 | Sharded ↔ non-sharded via smuggler / ETL / backup | Cross-topology data movement is the supported path; revision PK form must survive |

### Risk × scenario matrix

`●` = the scenario targets this risk. Phase-level detail lives in each scenario body ([scenarios.md](scenarios.md) / [soak.md](soak.md)).

| Risk | RV-1 | RV-2 | RV-3 | RP-1 | RP-2 | RP-3 | RPV-1 | RPV-2 | SK-1 | SK-2 |
|---|---|---|---|---|---|---|---|---|---|---|
| R-01 | — | — | — | — | — | — | ● | ● | ● | ● |
| R-02 \* | — | — | — | — | — | — | — | — | — | — |
| R-03 | ● | — | — | — | — | — | — | — | ● | — |
| R-04 | ● | — | — | — | ● | — | — | — | — | — |
| R-05 | ● | — | — | — | — | — | — | — | ● | — |
| R-06 | — | — | — | — | — | — | — | ● | ● | — |
| R-07 | ● | ● | — | — | — | — | ● | — | — | ● |
| R-08 | — | — | — | — | — | — | ● | — | — | ● |
| R-09 | — | — | — | ● | ● | — | — | — | — | — |
| R-10 | — | — | — | — | — | ● | — | ● | — | — |
| R-11 | — | — | — | — | ● | — | — | — | — | — |
| R-12 \*\* | — | — | — | — | — | — | — | — | — | — |
| R-13 | — | — | — | — | ● | — | — | — | ● | — |
| R-14 | — | — | — | — | ● | — | — | — | ● | — |
| R-15 | — | — | — | — | ● | — | — | — | ● | — |
| R-16 | — | — | — | — | — | — | — | ● | — | — |
| R-17 \*\*\* | — | — | — | — | — | — | — | — | — | — |
| R-18 | — | — | — | — | — | — | — | ● | — | — |
| R-19 | — | ● | — | — | — | — | — | — | — | — |
| R-20 | — | ● | ● | — | — | — | — | — | — | — |

\* **R-02** — no dedicated scenario; a residual Blake2b-128 collision is vanishingly rare and would surface as per-doc count drift (I-3) in any count-parity check (RV-1 phase 4 and every RPV convergence checkpoint).
\*\* **R-12** — deferred this PR; subscriptions are out of scope.
\*\*\* **R-17** — **coverage gap**: no current scenario restores a `v_62`-era or mid-migration backup into a `v_new` cluster. Needs a dedicated phase (e.g., an RPV-2 round that backs up on `v_62` and restores into `v_new`) before sign-off.

## False-positive mitigation

A "false positive" is a test failure that does not reflect a real defect — usually a timing race, a flaky transient, or a harness issue. This plan keeps the rate near zero by:

- **Two-budget waits.** Every assertion waits for `cluster-health` and `replication-quiet` budgets to both close before reading state. Snapshots taken mid-drain are never reported as divergence.
- **Bounded retry only on `LAG`.** Validators tag their result `OK | LAG | STUCK | CORRUPT`. `LAG` extends the budget once. `STUCK` and `CORRUPT` fail the first time — there is no "rerun until green."
- **Deterministic IDs and seeded RNG.** A re-run with the captured seed reproduces the exact write set and the exact chaos schedule. A failure that does not reproduce is filed as a chaos flake, never silenced. **Seed mechanism:** every workload (W-*) and every chaos-schedule primitive takes a single `seed` parameter; at T0 the resolved seed plus the fully-expanded chaos trace are written to the artifact bundle (`run-manifest.json`, via PD-6 `capture_artifact_bundle`). Replay re-supplies that seed — there is no hidden wall-clock or unseeded randomness in the workload or chaos layers.
- **Per-scenario isolation.** Unique `docker_network_name` per scenario. Tests cannot collide.
- **Pinned binaries.** `custom_build` URLs pinned to commit hashes; no `latest`.
- **Harness retries are narrow.** The harness may retry on provisioning errors (OOM during docker-up, network setup races). It never retries on a validator failure.
- **Forensic mode on failure.** Validators dump per-doc and per-revision diffs into the artifact directory the moment they fail, so the next run does not need to reproduce to be triaged.
