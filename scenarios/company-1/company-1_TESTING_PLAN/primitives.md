# company-1 Testing Plan — Toolbox Primitives

Every primitive in the plan has a namespaced ID:

| Prefix | Function |
|---|---|
| **PO-** | Operational — provisioning, fault injection, service control, replication-task control, backup / restore, hub-sink wiring |
| **PW-** | Writer — workload primitives that generate document / attachment / counter / TS writes |
| **PD-** | Diagnostic — read-only observation: stat queries, CV captures, health polls, cursor inspection |
| **PC-** | Convergence — gating validators that assert correctness invariants |

Status column: `existing` = already in the toolbox per `ansible-ravendb-chaos/CHEATSHEET.md`; `new` = needs implementation (full spec in [new_primitives.md](new_primitives.md)).

Scenarios are written in plain operational language and do not call primitives by name; this file is the index the orchestration-layer implementor uses to wire it all up.

## Operational (PO)

### Core infrastructure (`playbooks/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-1 | `provision_nodes` | existing | Spin up Docker containers in a multi-cluster layout |
| PO-2 | `install_ravendb` | existing | Deploy RavenDB binaries across all containers at a pinned version or custom build |
| PO-3 | `form_clusters` | existing | Merge provisioned nodes into unified clusters |
| PO-4 | `add_node` | existing | Provision and optionally join a single new node |
| PO-5 | `teardown_containers` | existing | Remove all containers and reset network / hosts configuration |
| PO-6 | `setup_ssh_targets` | existing | Install prerequisites on remote SSH hosts |
| PO-7 | `cleanup_ssh_targets` | existing | Uninstall RavenDB and remove network rules on remote SSH hosts |

### Network fault injection (`toolbox/network/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-8 | `cut_link` | existing | Block TCP between two specific nodes |
| PO-9 | `restore_link` | existing | Restore connectivity between two nodes |
| PO-10 | `partition_node` | existing | Isolate one node from all cluster peers |
| PO-11 | `heal_node` | existing | Restore one node's connectivity to all peers |
| PO-12 | `partition_set` | existing | Network partition between two node groups (use with `set_a` / `set_b` for cross-cluster partitions) |
| PO-13 | `heal_all` | existing | Clear all active iptables chaos rules cluster-wide |
| PO-14 | `inject_replication_lag` | new | Deterministic egress delay on a node's outbound replication |

### Service control (`toolbox/service/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-15 | `restart_ravendb` | existing | Stop / start RavenDB and wait for HTTPS readiness |
| PO-16 | `upgrade_node` | existing | Update RavenDB binary and restart one node |
| PO-17 | `remove_node` | existing | Decommission a node from its cluster |
| PO-18 | `force_cluster_asymmetry` | existing | Deploy different versions across cluster nodes |
| PO-19 | `kill_ravendb_hard` | new | SIGKILL the RavenDB process |

### Database operations (`toolbox/db/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-20 | `create_database` | existing | Provision a new replicated database |
| PO-21 | `delete_database` | existing | Drop a database and poll for completion |
| PO-22 | `configure_revisions` | existing | Apply a revisions configuration to a database |

### Replication tasks (`toolbox/tasks/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-23 | `set_mentor_node` | existing | Reassign a replication task's preferred sync peer |
| PO-24 | `mutate_replication_filter` | new | Change the filter spec on a running task |
| PO-25 | `poll_responsible_node` | new | Poll until the current responsible node of a replication task equals an expected tag (observe-only; pairs with PO-23) |
| PO-26 | `setup_etl` | new | Configure a RavenETL task between two databases |

### Backup & restore (`toolbox/backup/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-27 | `backup_database` | existing | Create a logical or snapshot backup to the filesystem |
| PO-28 | `restore_backup` | existing | Restore a database from a backup on a target cluster |

### Hub-sink (`scenarios/hub-sink/tasks/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PO-29 | `define_hub` | existing | Configure a hub cluster for pull replication |
| PO-30 | `attach_sinks` | existing | Configure sink clusters and establish pull replication legs |

## Writer (PW) — `toolbox/writes/`

| ID | Primitive | Status | Description |
|---|---|---|---|
| PW-1 | `write_docs` | existing | Bulk insert JSON documents with sequential IDs |
| PW-2 | `write_docs_interleaved` | existing | Round-robin insert across multiple ID prefixes |
| PW-3 | `write_docs_freeform` | existing | Insert random-ID documents without collection structure |
| PW-4 | `delete_docs` | existing | Remove documents by prefix or explicit ID list |
| PW-5 | `write_attachments` | existing | Attach binary blobs to existing documents |
| PW-6 | `write_counters` | existing | Increment / decrement counter values |
| PW-7 | `write_timeseries` | existing | Append time-series points or delete ranges |
| PW-8 | `restore_revision` | existing | Revert a document to a previous revision. Used inline by RV-1 phase 2's mixed-op loop. |

For `v_62`-form raw-CV revision seeding: composition of **PO-2** (install `v_62` binary on the seeding node) + **PW-1** (write_docs). No dedicated primitive.

## Diagnostic (PD)

### Stats & captures (`toolbox/diagnostic/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PD-1 | `diagnostic_replication` | existing | Enumerate incoming and outgoing replication streams. Also serves I-6 (no stuck task) — caller inspects the output for `Faulted` / `Error` state. |
| PD-2 | `diagnostic_partition_list` | existing | Display all active iptables rules across nodes |
| PD-3 | `diagnostic_capture_cv` | existing | Export change vectors from the database topology |
| PD-4 | `diagnostic_capture_doc_cv` | existing | Export per-document change vectors across specified nodes |
| PD-5 | `diagnostic_scan_fltr` | existing | Analyze captured CVs for anomalies |
| PD-6 | `capture_artifact_bundle` | new | One-shot forensic capture for a checkpoint — stats, replication, topology, database record, tombstones, optionally CV dump + cursor state + logs |

### Health polling (`toolbox/wait/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PD-7 | `wait_for_healthy` | existing | Poll until cluster responds and members communicate |
| PD-8 | `wait_for_rehab` | existing | Block until target enters `Promotables` / `Rehabs` state |
| PD-9 | `wait_for_member` | existing | Block until target becomes full cluster member |
| PD-10 | `wait_for_quiescence` | existing | Poll until the database reaches steady state (auto-discovers nodes, drops 404/500/503 nodes from the convergence set, two-budget poll) |

### Cursor forensics (`toolbox/diagnostic/cursor/`)

| ID | Primitive | Status | Description |
|---|---|---|---|
| PD-11 | `inspect_durable_cursor` | new | Read the Sink-owned `replication-source-cursor` records: key, value, direction. **Forensic** — used to debug I-12 failures. |
| PD-12 | `assert_cursor_advances_under_proof_barrier` | new | Cursor advances only after the receiver-local proof barrier is covered. **Forensic** — same role as PD-11. |

## Convergence / validation (PC)

All `new` — full spec in [new_primitives.md](new_primitives.md).

| ID | Primitive | Description |
|---|---|---|
| PC-1 | `assert_node_and_feature_parity` | All nodes in a cluster report a consistent binary identity and `DatabaseRecord.SupportedFeatures` |
| PC-2 | `compare_change_vectors` | Per-doc CV multiset equality across nodes (builds on PD-4) |
| PC-3 | `assert_no_orphan_revisions` | Trigger the existing `POST /admin/revisions/orphaned/adopt` and assert `AdoptedCount == 0` |
| PC-4 | `assert_equal_stats` | Aggregate `/stats` equality across nodes — document count, revision count, tombstone counts, attachment count, counter entries, time-series segments. Single validator for I-1 (counts) and for the count side of I-2. |
| PC-5 | `assert_filter_compliance` | Sink doc IDs match the configured filter (no leak) |
| PC-6 | `voron_growth_envelope` | Whole-database envelope via `/stats.SizeOnDisk` |
| PC-7 | `drift_detector` | Background loop: snapshots backlog + revisions count + workload writes; halts on monotonic growth without proportional workload growth |
| PC-8 | `assert_no_filter_skips` | No source doc matching the filter is missing on the sink (count parity). CV-layer guard is PC-10. |
| PC-9 | `assert_stored_item_cv_split` | After filtered ingress on a v_new receiver, every stored item CV is correctly shaped for the new lane. Generalized to take a node-target set — covers I-13's item-CV checks both on the receiver leader (part a) and across the receiver group's replicas (part c) in one primitive. |
| PC-10 | `assert_db_cv_order_side_only` | **The CV-boundary fix regression guard.** Receiver `LastDatabaseChangeVector` contains only receiver-group node entries. |
| PC-11 | `assert_old_lane_inert_on_v_old_peer` | When a v_new node connects to a v_62 peer, the new lane stays inert — no new-lane artifacts on the v_new side |
| PC-12 | `assert_marker_propagation` | Write a uniquely-IDed sentinel doc on the source, poll every replication destination until the marker appears (within budget). End-to-end "replication is flowing" check; the validator for I-5. |
