# EMR Testing Plan — Revisions Schema Migration & Filtered Replication

## Executive summary

This plan validates two high-risk changes shipping together on `revisions-new-schema`: a **storage-format change to every revision** (raw change-vector PK → 24-byte hashed PK, including the full v6.2 → v_new schema upgrade), and a **fix to a data-loss class of bugs in filtered replication** (items silently skipped, cursors advancing wrongly, failover losing its position). Both sit on the core storage and replication paths, so the bar is correctness *under failure*, not just the happy path.

It proves them with **8 deterministic chaos scenarios + 2 longevity soaks (10 in total)**, each ending in a machine-checked convergence assertion — pass/fail is mechanical, with no human judgment and no rerun-until-green. Coverage is enumerated, not asserted: **20 risks** and **13 correctness invariants**, each tied to the scenarios that exercise it. Confidence is gated in tiers — **the fix is mergeable once Tiers 0–2 are green** (~6 h 40 min of focused operator time); **release-ready** adds the two 2 h soaks.

**Readiness:** the suite is buildable today. One open item — where the new durable failover cursor is read from (PD-11) — gates only the failover and soak-cursor checks; two further items are tracked, neither a blocker. Detail in the [release gate](#release-gate--acceptance-criteria) and Open Items.

| At a glance | |
|---|---|
| Changes under test | Revision PK migration + filtered-replication fix |
| What proves it | 8 chaos scenarios (single-cluster / hub-sink / sharded) + 2 soaks = 10 |
| Pass / fail | Mechanical — `OK / LAG / STUCK / CORRUPT`, no rerun-until-green |
| Coverage | 20 risks · 13 invariants · every one mapped to scenarios |
| Release gate | Mergeable = Tiers 0–2 green; release-ready = + soaks |
| Cost | ~6 h 40 min focused + 2 soak days · operator-driven (no CI) |
| Open items | 1 gating (PD-11 cursor read surface) + 2 tracked non-blockers |

## What this is

A distributed-systems E2E test plan for two changes shipping in the `revisions-new-schema` branch:

1. **Revision PK migration.** 
	- Revisions move from a raw change-vector primary key to a 24-byte hashed PK. 
	- Migration is **lazy** — a document's legacy raw-CV revision rows are re-keyed to the hashed form over time, and the two forms coexist indefinitely. 
	- A cross-version connection runs the **old lane**: a `v_new` node down-converts to raw-CV so a `v_62` peer can store it (a `v_new` receiver stores incoming raw-CV as a legacy row). 
	- The full upgrade chain from v6.2 lands here too: `62000 → ... → 72001` runs as a sequence on a `v_62 → v_new` upgrade.
2. **Filtered-replication fix.** Filtered pull / push replication had a class of bugs where items could be silently skipped, cursors could advance wrongly, and failover could lose replication position. The fix ships on `v_new` and lane behavior is determined by node binary version — there is no runtime toggle. See the product design doc for the mechanics; this plan defines the validators and scenarios that prove it works.

Together these changes touch revision storage, every replication path, conflict resolution, backup / restore, smuggler, ETL, subscriptions, sharding, and the rolling-upgrade machinery.

## File index

| File | Purpose |
|---|---|
| [context.md](context.md) | Philosophy, risk analysis, taxonomy, topology + version + fault matrices, workloads, configs, invariants, false-positive mitigation |
| [coverage.md](coverage.md) | Visual matrices: scenarios × subsystems, scenarios × invariants, version-pair coverage, lane behavior coverage, entity coverage |
| [primitives.md](primitives.md) | Flat catalog of orchestration primitives (existing + proposed) with one-line descriptions |
| [new_primitives.md](new_primitives.md) | Full spec for proposed primitives — purpose, parameters, why-needed, implementation order |
| [scenarios.md](scenarios.md) | 8 deterministic chaos scenarios across Class 1 (single cluster), Class 2 (hub-sink replication), Class 3 (sharded) |
| [soak.md](soak.md) | 2 longevity scenarios (SK-1 and SK-2), each ≤ 2 h per variant |

## Testing philosophy (1-minute version)

1. **Convergence is a first-class invariant.** Every scenario reaches a verifiable quiescent state and asserts byte-equality across peers: counts, change vectors, and the consolidated per-doc stats (attachments + counters + time-series).
2. **Distinguish lag from divergence.** Two-budget waits (cluster-health + replication-quiet). Validators tag failures `OK | LAG | STUCK | CORRUPT`; only the last two fail without an extension.
3. **Failure is the workload.** Partitions, restarts, mentor swaps, and filter mutations are interleaved with reads and writes by design.
4. **Mixed-form is the default.** Raw-CV rows and hashed-form rows coexist; tests seed both, replicate both, partition through both.
5. **Read-after-write is not enough.** Tests assert no orphan revisions (via the adopt-path probe), no docs skipped under filter, no stuck replication. Authoritative duplicate-revision detection runs only in RV-1 phase 4; every other scenario uses the per-doc count parity proxy.
6. **Deterministic seeds.** Soak failures are replayable from a captured seed.
7. **Operational-language recipes.** Steps read in plain English ("provision the cluster, partition node `1a`"); [primitives.md](primitives.md) and [new_primitives.md](new_primitives.md) wire the calls.
8. **Mega-scenarios layer lateral chaos.** Each scenario chains multiple chaos primitives in sequence; phase N runs on residual state from phase N − 1. Failure forensics use per-phase checkpoints to localize.

## Execution model

All scenarios are **operator-driven** — there is no CI gating. The 10-scenario catalog is small enough to hold in your head; the suggested phases below order them by smoke → filtered-replication fix → migration depth → soak.

## Execution sequence

Operator capacity assumption: **at most 3 scenarios in parallel** (each scenario uses its own docker network — 3-cluster scenarios consume ~18 GB RAM and ~90 GB disk in aggregate). `solo` scenarios are resource-heavy or long-running and run alone. Phases run **fail-fast** in order; finish a phase before starting the next, and each phase unlocks a release-gate tier.

| Phase | Scenarios (in order) | Wall | Unlocks |
|---|---|---|---|
| **A** Smoke | RP-1 | ~12 min | Basics sane (Tier 0) |
| **B** Filtered-replication fix | RP-2 `solo` → RP-3 | ~100 min | Fix #1 + #2 regression-clean (Tier 1) |
| **C** Migration & rolling upgrade | RV-2 → RV-3 → RPV-2 → RPV-1 → RV-1 `solo` | ~290 min | PK migration correct end-to-end (Tier 2) |
| **D** Soak | SK-1 `solo` → SK-2 `solo` | 2 h + 2 h | Release-ready (Tier 3) |

Smoke + fix regression (A + B) ≈ 1 h 50 min focused wall. Stop after any phase that fails. Within Phase C, RV-2 must precede RV-3 (RV-3 consumes RV-2's end state).

## Suggested operator sessions

| Session | Goal | Scenarios | Wall |
|---|---|---|---|
| **Smoke** | Confirm the replication fix is sane on a clean cluster | RP-1 | ~12 min |
| **Fix regression** | Validate the filtered-replication fix end-to-end | A + B (RP-1, RP-2, RP-3) | ~1 h 50 min |
| **Full focused pass** | Everything except soak | A + B + C (8 scenarios) | ~6 h 40 min |
| **Soak campaign** | All 2 h soaks | SK-1 + SK-2 | 4 h (one per day) |
| **Complete pass** | Everything | A + B + C + D | ~6 h 40 min focused + 2 calendar days of soak |

## Release gate / acceptance criteria

The suite is operator-driven (no CI). A run is judged mechanically: every validator returns `OK | LAG | STUCK | CORRUPT`. Only `OK` passes; a single `LAG` budget extension is allowed; `STUCK` or `CORRUPT` fails. **No rerun-until-green** — a reproduced `CORRUPT` is a blocker; a failure that does not reproduce from its captured seed is filed as a chaos flake, never silenced.

The gate is tiered — each tier must be fully green before the claim it unlocks can be made:

| Tier | Scenarios | Claim unlocked |
|---|---|---|
| **0 — Smoke** | Phase A — RP-1 | The CV-boundary fix is sane on a clean cluster. Any `CORRUPT` here → stop, do not proceed to later tiers. |
| **1 — Filtered-replication fix validated** | Phase B — RP-2, RP-3 (all `OK`) | The CV-boundary + failover-cursor fix is regression-clean. |
| **2 — PK migration validated** | Phase C — RV-2, RV-3, RPV-2, RPV-1, RV-1 (all `OK`, incl. RV-1 phase-4 count-parity sweep) | The revision PK migration is correct end-to-end, incl. cross-version and rolling upgrade. |
| **3 — Release-ready** | Phase D — SK-1, SK-2 (both clean: no drift halt, all quiescence checkpoints pass, storage envelopes hold) | Shippable. |

**"The fix is validated and mergeable" = Tiers 0–2 green.** "Release-ready" additionally requires **Tier 3**.

**Pre-handoff status.** One open item gates the *full* suite: **PD-11's read surface** — where the `v_new` branch persists the durable failover cursor (and therefore which endpoint reads it) is still **TBD**; compare-exchange (`GET /databases/{db}/cmpxchg?key=<key>`) is the leading candidate but unconfirmed against the implementation. It gates RP-3 and the SK-1 / SK-2 cursor checks only — every other scenario, primitive, and tier is implementable today. Two further tracked items (not blockers; see Open Items in [new_primitives.md](new_primitives.md)): the **R-18 cross-version-restore coverage gap** and the **drift-detector recovery-window** design point.
