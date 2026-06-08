# EMR Testing Plan — Coverage Audit

This file answers two questions:

1. Does every code-path subsystem the two changes touch have at least one primary scenario covering it?
2. Does every version pair we care about have at least one scenario walking past it?

Invariant definitions live in [context.md](context.md). Validator details live in [primitives.md](primitives.md) / [new_primitives.md](new_primitives.md). This file does **not** restate them.

## Cell legend

- `●` primary focus — scenario is designed to exercise this dimension
- `○` secondary coverage — exercised as a side effect, not the main thing
- `—` not exercised

## Subsystem inventory

| Group | Subsystem |
|---|---|
| **A** | Storage & compaction (voron prefix scans, table compaction over mixed PK forms) |
| **B** | Revisions API (`Put` / `Delete` / `Get` / `Revert` / `RevisionsBin` under load) |
| **C** | Conflicts (split-brain resolution, conflict-revision creation) |
| **D** | Replication wire (internal cluster + hub-sink wire format and batch sequencing) |
| **E** | Smuggler (export / import across versions) |
| **F** | Backup / Restore (snapshot vs smuggler; restore-during-replication) |
| **G** | Schema upgrade (mixed-mode reads, restart mid-upgrade, full chain `v_62 → v_new`) |
| **H** | ETL (RavenETL emitters). *Revision subscriptions are out of scope this PR — the `open_subscription` stub fails loudly with implementation guidance.* |
| **I** | Sharding (orchestrator + per-shard internal replication) |
| **J** | Topology + filter (mentor reassignment, task movement, filter mutation, task lifecycle) |
| **K** | CV boundary (the fix on v_new receivers — stored item CV, DB CV, conflict / echo, replica preservation) |
| **L** | Failover cursor (durable Sink-owned cursor on v_new sinks — handshake, advance under crashes and takeovers) |

## Subsystem × scenario matrix

| Scenario | A | B | C | D | E | F | G | H | I | J | K | L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **RV-1** | ● | ● | ○ | ○ | — | — | ● | — | — | ○ | — | — |
| **RV-2** | ● | ○ | — | ● | ● | ● | ● | — | ● | ○ | — | — |
| **RV-3** | ● | ○ | — | ● | ● | ● | — | ● | ● | ○ | — | — |
| **RP-1** | — | ● | — | ● | — | — | — | — | — | ○ | ● | — |
| **RP-2** | ○ | ○ | ● | ● | — | — | — | — | — | ● | ● | ○ |
| **RP-3** | — | — | — | ● | — | — | — | — | — | ● | ○ | ● |
| **RPV-1** | ○ | ○ | — | ● | — | — | ● | — | — | ○ | ○ | — |
| **RPV-2** | ● | ● | — | ● | ● | ● | ○ | — | — | ● | — | — |
| **SK-1** | ○ | ● | ○ | ● | — | — | — | — | — | ● | ○ | — |
| **SK-2** | ○ | ○ | ○ | ● | — | — | ● | — | — | ● | ○ | — |

## Subsystem audit

Every column has at least one `●` — nothing is uncovered.

| Column | Primary coverage |
|---|---|
| **A** Storage & compaction | RV-1, RV-2, RV-3, RPV-2 |
| **B** Revisions API | RV-1, RP-1, RPV-2, SK-1 |
| **C** Conflicts | RP-2 |
| **D** Replication wire | RV-2, RV-3, RP-1, RP-2, RP-3, RPV-1, RPV-2, SK-1, SK-2 |
| **E** Smuggler | RV-2, RV-3, RPV-2 |
| **F** Backup / Restore | RV-2, RV-3, RPV-2 |
| **G** Schema upgrade | RV-1, RV-2, RPV-1, SK-2 |
| **H** ETL | RV-3 (subscriptions deferred this PR — no scenario) |
| **I** Sharding | RV-2, RV-3 |
| **J** Topology + filter | RP-2, RP-3, RPV-2, SK-1, SK-2 |
| **K** CV boundary | RP-1, RP-2 |
| **L** Failover cursor | RP-3 |

## Version-pair coverage

Organized by **what gets exercised**, not just source/target arithmetic. Intra-cluster mid-roll snapshots hit both directions of a version pair simultaneously and are listed accordingly.

| Pair | Where exercised |
|---|---|
| **`v_new` ↔ `v_new`** (peer-to-peer on the new code; both directions) | All-`v_new` scenarios: RP-1, RP-2, RP-3, RPV-2, RV-3, SK-1 |
| **`v_62` ↔ `v_62`** (peer-to-peer on the old code) | Initial state of every upgrade scenario, before the first upgrade fires (RPV-1, RV-1, RV-2, SK-2) |
| **`v_62` ↔ `v_new` — intra-cluster mid-roll** (mixed binaries within the same cluster; both directions simultaneously) | RV-1 phase 1, SK-2 |
| **`v_62` ↔ `v_new` — cross-cluster mid-roll** (the cross-cluster T3 surface; both directions across the three checkpoints) | RPV-1 (all 3 variants) |
| **`v_62` ↔ `v_new` — sharded internal** (mixed binaries among shard replicas during rolling upgrade) | RV-2 phase (a) |
| **Downgrade** (`v_new → v_62`) | Not covered at the scenario level — the binary refuses to come up. Refusal contract is covered by a unit test outside this plan. |

## Entity coverage

Every chaos scenario writes through at least one entity surface. The matrix lists what each scenario *actively writes* against; passive replication of the same entities is implicit at the receivers.

| Scenario | Docs | Doc tombstones | Revisions | Revision tombstones | Attachments | Counters | Time-series | Conflict docs |
|---|---|---|---|---|---|---|---|---|
| RV-1 | ● | ● | ● | ● | ● (phase 2) | — | — | — |
| RV-2 | ● | ● | ● | ● | — | — | — | — |
| RV-3 | ● | ● | ● | ● | — | — | — | — |
| RP-1 | ● | ● | ● | ● | ● | ● | ● | ● |
| RP-2 | ● | ● | ● | ● | — | — | — | ● |
| RP-3 | ● | — | ● | — | — | — | — | — |
| RPV-1 | ● | ● | ● | ● | ● | ● | ● | — |
| RPV-2 | ● | ● | ● | ● | ● | ● | ● | — |
| SK-1 | ● | ● | ● | ● | ● | ● | ● | — |
| SK-2 | ● | ● | ● | ● | ● | ● | ● | — |
