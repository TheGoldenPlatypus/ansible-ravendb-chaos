# Cheatsheet

Copy-paste commands to exercise every playbook in the repo, including each optional-var variant.
Run from the repo root (`ansible-ravendb-chaos/`).

**Noise control:** loopy tools (`write_docs`, `delete_docs`, `diagnostic_doc_id_set_parity`,
`diagnostic_revision_count_parity`, `_wait_for_*_attempt`, etc.) default to `quiet=true` and hide
per-item PUT/GET lines. Add `-e quiet=false` to any command below to see the full per-item firehose
when debugging a single tool. Final `Done` / PASS / FAIL summaries are always visible regardless.

---

## 1. Core infrastructure (`playbooks/`)

### `provision_nodes.yml`

```bash
# Default: 1 cluster x 3 nodes = 3 containers (1a, 1b, 1c).
ansible-playbook playbooks/provision_nodes.yml -K

# Scale cluster count: 2 clusters x 3 nodes = 6 containers.
ansible-playbook playbooks/provision_nodes.yml -K -e clusters_count=2

# Scale both dimensions and shrink per-container memory.
ansible-playbook playbooks/provision_nodes.yml -K -e clusters_count=3 -e nodes_per_cluster=5 -e container_memory=1500m

# Use a different docker network + image (e.g. testing a different base OS).
ansible-playbook playbooks/provision_nodes.yml -K -e docker_network_name=othernet -e docker_image=ubuntu2404-ansible
```

### `install_ravendb.yml`

```bash
# Default: install group_vars rdb_version on every existing container.
ansible-playbook playbooks/install_ravendb.yml -K

# Pin to a specific RavenDB version.
ansible-playbook playbooks/install_ravendb.yml -K -e rdb_version=6.2.15

# Install a CUSTOM build from a URL (requires --skip-tags download).
ansible-playbook playbooks/install_ravendb.yml -K -e custom_build=https://internal.example.com/raven-feature-branch.deb --skip-tags download

# Install a CUSTOM build from a local .deb on the controller.
ansible-playbook playbooks/install_ravendb.yml -K -e custom_build=/tmp/ravendb-7.2.2.deb --skip-tags download

# Point at a different cert folder than the group_vars default.
ansible-playbook playbooks/install_ravendb.yml -K -e cert_dir=/path/to/certs
```

### `form_clusters.yml`

```bash
# Default: merge each cluster's nodes into one cluster (needs sudo for /etc/hosts).
ansible-playbook playbooks/form_clusters.yml -K

# Override the cert folder.
ansible-playbook playbooks/form_clusters.yml -K -e cert_dir=/path/to/certs
```

### `add_node.yml`

```bash
# TEST 1 -- standalone bootstrapped 1-node cluster (admin-cert registered, has own tag A).
ansible-playbook playbooks/add_node.yml -K -e node_name=solo1

# TEST 2 -- standalone PASSIVE (no bootstrap, no cert) ready to be added via Studio later.
ansible-playbook playbooks/add_node.yml -K -e node_name=xyz3 -e passive=true

# TEST 3 -- grow cluster 1 with a 4th node (convention-following name; tag D auto-derived).
ansible-playbook playbooks/add_node.yml -K -e node_name=1d -e join_to=1a

# TEST 4 -- non-convention name + explicit tag, joining cluster 1's leader.
ansible-playbook playbooks/add_node.yml -K -e node_name=myextra -e join_to=1a -e node_tag=E

# TEST 5 -- standalone node from a custom local .deb (dev branch artifact).
ansible-playbook playbooks/add_node.yml -K -e node_name=customsolo -e custom_build=/tmp/ravendb-7.2.2.deb --skip-tags download

# TEST 6 -- custom .deb + join an existing cluster with explicit tag.
ansible-playbook playbooks/add_node.yml -K -e node_name=customjoin -e custom_build=/tmp/ravendb-7.2.2.deb -e join_to=1a -e node_tag=F --skip-tags download

# TEST 7 -- override container memory cap.
ansible-playbook playbooks/add_node.yml -K -e node_name=1e -e join_to=1a -e container_memory=1500m
```

### `teardown_containers.yml`

```bash
# Removes containers + docker network + lab_backups volume + captures/ + leftover W-1 + /etc/hosts block.
ansible-playbook playbooks/teardown_containers.yml -K
```

---

## 2. Toolbox (`toolbox/`)

### `cut_link.yml`

```bash
# Cut all TCP between two containers (forces TCP reset).
ansible-playbook toolbox/network/cut_link.yml -K -e node_a=1a -e node_b=1b
```

### `restore_link.yml`

```bash
# Undo a previous cut_link between two containers.
ansible-playbook toolbox/network/restore_link.yml -K -e node_a=1a -e node_b=1b
```

### `partition_node.yml`

```bash
# Cut one node off from every cluster peer in one go.
ansible-playbook toolbox/network/partition_node.yml -K -e target=1c
```

### `heal_node.yml`

```bash
# Undo a previous partition_node by restoring every peer link.
ansible-playbook toolbox/network/heal_node.yml -K -e target=1c
```

### `restart_ravendb.yml`

```bash
# Restart the RavenDB service inside one container and wait for HTTPS.
ansible-playbook toolbox/service/restart_ravendb.yml -K -e target=1b

# Same, with a custom wait budget (seconds).
ansible-playbook toolbox/service/restart_ravendb.yml -K -e target=1b -e timeout_secs=240
```

### `write_docs.yml`

```bash
# Write 50 docs with the default id_prefix (micro/doc/0..49).
ansible-playbook toolbox/writes/write_docs.yml -K -e target=1a -e db_name=Tenants -e count=50

# Write 20 docs with a custom id prefix.
ansible-playbook toolbox/writes/write_docs.yml -K -e target=1a -e db_name=Tenants -e count=20 -e id_prefix=probe/burst1
```

### `write_docs_interleaved.yml`

```bash
# Round-robin write across two prefixes (A-0, B-0, A-1, B-1, ...).
ansible-playbook toolbox/writes/write_docs_interleaved.yml -K -e target=1a -e db_name=Tenants -e count=20 -e '{"prefixes":["tenants/cluster2/x","other/x"]}'

# Round-robin across three prefixes (X-0, Y-0, Z-0, X-1, ...).
ansible-playbook toolbox/writes/write_docs_interleaved.yml -K -e target=1a -e db_name=Tenants -e count=15 -e '{"prefixes":["X","Y","Z"]}'
```

### `diagnostic_doc_count.yml`

```bash
# Print CountOfDocuments from /stats on a target node.
ansible-playbook toolbox/diagnostic/diagnostic_doc_count.yml -K -e target=1b -e db_name=Tenants
```

### `create_database.yml`

```bash
# Create the DB on every node in the cluster (default replication_factor=3).
ansible-playbook toolbox/db/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants

# Create a single-node DB (replication_factor=1).
ansible-playbook toolbox/db/create_database.yml -K -e cluster_leader=1a -e db_name=MyTestDb -e replication_factor=1
```

### `delete_database.yml`

```bash
# Delete a DB and poll until it's gone (default 60s wait budget).
ansible-playbook toolbox/db/delete_database.yml -K -e cluster_leader=1a -e db_name=Tenants

# Same, with a longer wait budget.
ansible-playbook toolbox/db/delete_database.yml -K -e cluster_leader=1a -e db_name=Tenants -e timeout_secs=180
```

### `remove_node.yml`

```bash
# Remove the node with cluster tag D from cluster 1.
ansible-playbook toolbox/service/remove_node.yml -K -e cluster_leader=1a -e target_tag=D
```

### `upgrade_node.yml`

```bash
# Upgrade one node to a specific version from daily-builds.
ansible-playbook toolbox/service/upgrade_node.yml -K -e target=1a -e rdb_version=7.2.3

# Upgrade one node from a custom .deb (dev branch artifact).
ansible-playbook toolbox/service/upgrade_node.yml -K -e target=1a -e custom_build=/tmp/raven-branch.deb --skip-tags download

# Upgrade with a longer wait budget for the node to come back.
ansible-playbook toolbox/service/upgrade_node.yml -K -e target=1a -e rdb_version=7.2.3 -e timeout_secs=300

# Rolling upgrade across a 3-node cluster, with a health gate between each step.
for n in 1a 1b 1c; do ansible-playbook toolbox/service/upgrade_node.yml -K -e target=$n -e rdb_version=7.2.3; ansible-playbook toolbox/wait/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity; done
```

### `diagnostic_replication.yml`

```bash
# Dump incoming + outgoing replication connections for the DB on a node.
ansible-playbook toolbox/diagnostic/diagnostic_replication.yml -K -e target=2a -e db_name=Tenants
```

### `wait_for_healthy.yml`

```bash
# Cheap recovery check (entry-point node responds + members can talk).
ansible-playbook toolbox/wait/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity

# Same, with a longer total wait budget.
ansible-playbook toolbox/wait/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity -e max_wait=180
```

### `wait_for_rehab.yml`

```bash
# Block until 1b enters Promotables/Rehabs on Tenants's topology (fails if it doesn't within 120s).
ansible-playbook toolbox/wait/wait_for_rehab.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b

# Longer wait budget.
ansible-playbook toolbox/wait/wait_for_rehab.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b -e timeout_secs=300
```

### `wait_for_member.yml`

```bash
# Block until 1b is back as a full Member of Tenants's topology (default 300s budget).
ansible-playbook toolbox/wait/wait_for_member.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b

# Longer wait budget for slow rehab phases.
ansible-playbook toolbox/wait/wait_for_member.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b -e timeout_secs=600
```

### `write_docs_freeform.yml`

```bash
# Write 1 freeform doc to 2a (random GUID id, null collection).
ansible-playbook toolbox/writes/write_docs_freeform.yml -K -e target=2a -e db_name=Tenants -e count=1

# Write 5 freeform docs to 2b.
ansible-playbook toolbox/writes/write_docs_freeform.yml -K -e target=2b -e db_name=Tenants -e count=5
```

### `delete_docs.yml`

```bash
# Delete 50 docs that match the default write_docs.yml prefix.
ansible-playbook toolbox/writes/delete_docs.yml -K -e target=1a -e db_name=Tenants -e id_prefix=micro/doc -e count=50

# Delete an explicit list of ids.
ansible-playbook toolbox/writes/delete_docs.yml -K -e target=1a -e db_name=Tenants -e '{"ids":["users/1","users/42"]}'
```

### `force_cluster_asymmetry.yml`

```bash
# Asymmetric hub: 1b on 7.2.3, 1c on a dev .deb, 1a untouched.
ansible-playbook toolbox/service/force_cluster_asymmetry.yml -K -e '{"version_map":{"1b":"7.2.3","1c":"/tmp/raven-feature-branch.deb"}}'

# Whole hub cluster to the same new version (regular rolling upgrade in one shot).
ansible-playbook toolbox/service/force_cluster_asymmetry.yml -K -e '{"version_map":{"1a":"7.2.3","1b":"7.2.3","1c":"7.2.3"}}'

# Hub upgraded, sink left on old version -- cross-version replication test.
ansible-playbook toolbox/service/force_cluster_asymmetry.yml -K -e '{"version_map":{"1a":"7.2.3","1b":"7.2.3","1c":"7.2.3"}}'
```

### `partition_set.yml`

```bash
# isolate hub mentor 1a from the other 8 nodes (cross-cluster).
ansible-playbook toolbox/network/partition_set.yml -K -e '{"set_a":["1a"],"set_b":["1b","1c","2a","2b","2c","3a","3b","3c"]}'

# isolate Sink_A from Hub + Sink_B.
ansible-playbook toolbox/network/partition_set.yml -K -e '{"set_a":["2a","2b","2c"],"set_b":["1a","1b","1c","3a","3b","3c"]}'

# Overlapping sets -- self-pairs auto-skipped (here: 3 effective pairs 1a-1b, 1a-1c, 1b-1c).
ansible-playbook toolbox/network/partition_set.yml -K -e '{"set_a":["1a","1b"],"set_b":["1b","1c"]}'

# SSH mode (same syntax + inventory flag).
ansible-playbook -i inventory/ssh_hosts.yml toolbox/network/partition_set.yml -K \
    -e '{"set_a":["2a","2b","2c"],"set_b":["1a","1b","1c"]}'
```

### `heal_all.yml`

```bash
# Flush every chaos rule on every container in one shot.
ansible-playbook toolbox/network/heal_all.yml -K

# SSH mode.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/network/heal_all.yml -K

# Surgical override -- only heal these two.
ansible-playbook toolbox/network/heal_all.yml -K -e '{"targets":["1a","1b"]}'
```

### `diagnostic_partition_list.yml`

```bash
# List every active chaos iptables rule across the lab.
ansible-playbook toolbox/diagnostic/diagnostic_partition_list.yml

# SSH mode.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/diagnostic/diagnostic_partition_list.yml -K

# Restrict the listing to a subset.
ansible-playbook toolbox/diagnostic/diagnostic_partition_list.yml -e '{"targets":["1a","1b"]}'
```

### `diagnostic_capture_cv.yml`

```bash
# Scope to cluster 1 (nodes is REQUIRED -- multi-cluster lab needs explicit scoping).
ansible-playbook toolbox/diagnostic/diagnostic_capture_cv.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}'

# Custom output dir.
ansible-playbook toolbox/diagnostic/diagnostic_capture_cv.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' -e output_dir=captures/m1-baseline
```

### `diagnostic_capture_doc_cv.yml`

```bash
# Capture per-doc CVs for 3 ids on cluster 1 (3 ids x 3 nodes = 9 files).
ansible-playbook toolbox/diagnostic/diagnostic_capture_doc_cv.yml \
    -e db_name=Tenants \
    -e '{"ids":["micro/doc/0","micro/doc/25","micro/doc/49"],"nodes":["1a","1b","1c"]}'

# Custom output dir.
ansible-playbook toolbox/diagnostic/diagnostic_capture_doc_cv.yml \
    -e db_name=Tenants -e output_dir=captures/m1-docs \
    -e '{"ids":["users/a-1"],"nodes":["1a","1b","1c"]}'
```

### `diagnostic_scan_fltr.yml`

```bash
# Strict scan (default) -- fails non-zero on any FLTR hit.
ansible-playbook toolbox/diagnostic/diagnostic_scan_fltr.yml -e capture_dir=captures/m1-baseline

# Report-only -- prints LEAK lines but exits 0.
ansible-playbook toolbox/diagnostic/diagnostic_scan_fltr.yml -e capture_dir=captures/m1-baseline -e strict=false
```

### `wait_for_quiescence.yml`

```bash
# Scope to cluster 1.  nodes is REQUIRED (otherwise a hub-sink lab would try to converge
# the hub's CVs against the sink's, which never settles).  Drops nodes returning
# 404/500/503 from the convergence set on the first poll.
ansible-playbook toolbox/wait/wait_for_quiescence.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}'

# Tighter budget.
ansible-playbook toolbox/wait/wait_for_quiescence.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' \
    -e timeout=60 -e poll_interval=2
```

### `wait_for_docs_drain.yml`

```bash
# Per-node "writes have flushed" check -- CV unchanged across two consecutive polls.
ansible-playbook toolbox/wait/wait_for_docs_drain.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}'

# Faster smoke (default poll_interval=3 ⇒ wall ≥ 6s; with poll_interval=1 ⇒ wall ≥ 2s).
ansible-playbook toolbox/wait/wait_for_docs_drain.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' \
    -e timeout=20 -e poll_interval=1
```

### `wait_for_conflicts_resolved.yml`

```bash
# Poll /replication/conflicts until every node reports zero. Default 60s budget.
ansible-playbook toolbox/wait/wait_for_conflicts_resolved.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}'

# Longer budget.
ansible-playbook toolbox/wait/wait_for_conflicts_resolved.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' -e timeout=90
```

### `wait_for_leader.yml`

```bash
# Any leader will do -- block until /cluster/topology reports one. 60s default budget.
ansible-playbook toolbox/wait/wait_for_leader.yml -e target=1b

# Pin to a specific node tag (e.g. after a deliberate mentor flip).
ansible-playbook toolbox/wait/wait_for_leader.yml -e target=1b -e expected_leader=C
```

### `wait_for_marker_propagation.yml`

```bash
# Write a marker on 1a, poll 1b and 1c until it appears. 60s default budget.
ansible-playbook toolbox/wait/wait_for_marker_propagation.yml \
    -e db_name=Tenants -e source=1a -e '{"targets":["1b","1c"]}'

# Longer budget for slow replication scenarios.
ansible-playbook toolbox/wait/wait_for_marker_propagation.yml \
    -e db_name=Tenants -e source=1a -e '{"targets":["1b","1c"]}' -e timeout_secs=180
```

### `wait_for_workload_started.yml`

```bash
# Block until a background workload's pidfile shows up (use after a fire-and-forget launch).
ansible-playbook toolbox/workloads/wait_for_workload_started.yml \
    -e pidfile=/tmp/w1-1a-db1.pid
```

### `stop_workload.yml`

```bash
# TERM (grace window), then KILL.  No-op if the workload already exited.
ansible-playbook toolbox/workloads/stop_workload.yml \
    -e pidfile=/tmp/w1-1a-db1.pid

# Custom grace window before KILL.
ansible-playbook toolbox/workloads/stop_workload.yml \
    -e pidfile=/tmp/w1-1a-db1.pid -e grace_secs=5
```

### `diagnostic_doc_count_parity.yml`

```bash
# Assert every node in cluster 1 reports the same CountOfDocuments.
ansible-playbook toolbox/diagnostic/diagnostic_doc_count_parity.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_doc_id_set_parity.yml`

```bash
# Probe a deterministic id range (smoke/i1/0..49); each id must be present-on-all or absent-on-all.
ansible-playbook toolbox/diagnostic/diagnostic_doc_id_set_parity.yml \
    -e db_name=Tenants -e id_prefix=smoke/i1 -e count=50 \
    -e '{"nodes":["1a","1b","1c"]}'

# Or explicit id list.
ansible-playbook toolbox/diagnostic/diagnostic_doc_id_set_parity.yml \
    -e db_name=Tenants \
    -e '{"ids":["users/1","users/42"],"nodes":["1a","1b","1c"]}'
```

### `diagnostic_revision_count_parity.yml`

```bash
# Per-id revision count parity across nodes.
ansible-playbook toolbox/diagnostic/diagnostic_revision_count_parity.yml \
    -e db_name=Tenants -e id_prefix=smoke/i1 -e count=20 \
    -e '{"nodes":["1a","1b","1c"]}'

# Strict mode: also assert every id has exactly N revisions everywhere.
ansible-playbook toolbox/diagnostic/diagnostic_revision_count_parity.yml \
    -e db_name=Tenants -e id_prefix=smoke/i1 -e count=20 -e expected_count=5 \
    -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_schema_version.yml`

```bash
# Dump per-node FullVersion (no asserts).
ansible-playbook toolbox/diagnostic/diagnostic_schema_version.yml \
    -e '{"nodes":["1a","1b","1c"]}'

# Endpoint check after a rolling upgrade -- assert parity + expected major.minor.
ansible-playbook toolbox/diagnostic/diagnostic_schema_version.yml \
    -e require_parity=true -e expected_version=7.2 \
    -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_size_envelope.yml`

```bash
# First call -- captures baseline (file doesn't exist yet).
ansible-playbook toolbox/diagnostic/diagnostic_size_envelope.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' \
    -e baseline_file=$PWD/captures/size-baseline-Tenants.json

# Later -- checks current SizeOnDisk against the captured baseline (file now exists).
ansible-playbook toolbox/diagnostic/diagnostic_size_envelope.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' \
    -e baseline_file=$PWD/captures/size-baseline-Tenants.json

# Tighter envelope (default is 300%).
ansible-playbook toolbox/diagnostic/diagnostic_size_envelope.yml \
    -e db_name=Tenants -e '{"nodes":["1a","1b","1c"]}' \
    -e baseline_file=$PWD/captures/size-baseline-Tenants.json \
    -e max_growth_pct=25
```

### `write_attachments.yml`

```bash
# 3 attachments named data/0..data/2 on existing docs files/hub/0..files/hub/2.
ansible-playbook toolbox/writes/write_attachments.yml -K -e target=1a -e db_name=Tenants -e count=3 \
    -e doc_id_prefix=files/hub -e attachment_name=data
```

### `write_counters.yml`

```bash
# Increment Likes by 1 once.
ansible-playbook toolbox/writes/write_counters.yml -K -e target=1a -e db_name=Tenants -e doc_id=users/0

# Increment Likes by 2 three times (total +6).
ansible-playbook toolbox/writes/write_counters.yml -K -e target=1a -e db_name=Tenants \
    -e doc_id=users/0 -e counter_name=Likes -e delta=2 -e repeat=3
```

### `write_timeseries.yml`

```bash
# Append 5 Heartrate entries at 1-minute spacing.
ansible-playbook toolbox/writes/write_timeseries.yml -K -e target=1a -e db_name=Tenants \
    -e doc_id=users/0 -e count=5 -e interval_seconds=60 \
    -e start_timestamp=2026-05-28T12:00:00.000Z

# Delete-range mode (inclusive on both ends).
ansible-playbook toolbox/writes/write_timeseries.yml -K -e target=1a -e db_name=Tenants \
    -e doc_id=users/0 -e ts_name=Heartrate \
    -e delete_from=2026-05-28T12:01:00.000Z -e delete_to=2026-05-28T12:03:00.000Z
```

### `configure_revisions.yml`

```bash
# Enable revisions with default MinimumRevisionsToKeep=100.
ansible-playbook toolbox/db/configure_revisions.yml -K -e target=1a -e db_name=Tenants

# Override the count.
ansible-playbook toolbox/db/configure_revisions.yml -K -e target=1a -e db_name=Tenants -e minimum_revisions=50
```

### `restore_revision.yml`

```bash
# Restore an older revision as the new live doc. Get revision_cv from /revisions?id=... or
# from a prior diagnostic_capture_doc_cv capture.
ansible-playbook toolbox/writes/restore_revision.yml -K -e target=1a -e db_name=Tenants \
    -e doc_id=files/1 -e revision_cv='A:3-uQvp4csQpESZNSbifH8hxQ'
```

### `write_docs_revisions.yml`

```bash
# Seed 100 docs with 5 distinct revisions each (500 PUTs total) -- needed because write_docs.yml
# dedups identical bodies into a single revision.  Requires revisions config already enabled.
ansible-playbook toolbox/writes/write_docs_revisions.yml \
    -e target=1a -e db_name=db1 -e count=100 -e revs_per_doc=5

# 50k-revision example
ansible-playbook toolbox/writes/write_docs_revisions.yml \
    -e target=1a -e db_name=db1 -e count=10000 -e revs_per_doc=5

# Custom prefix
ansible-playbook toolbox/writes/write_docs_revisions.yml \
    -e target=1a -e db_name=db1 -e count=20 -e revs_per_doc=3 -e id_prefix=probe/r1
```

### `set_mentor_node.yml`

```bash
# Hub pull-replication task -> mentor=B.
ansible-playbook toolbox/tasks/set_mentor_node.yml -K -e target=1a -e db_name=Tenants \
    -e task_name=bidirectional-tenants -e task_type=hub -e mentor_node=B

# Sink task -> mentor=C.
ansible-playbook toolbox/tasks/set_mentor_node.yml -K -e target=2a -e db_name=Tenants \
    -e task_name=<sink-task-name> -e task_type=sink -e mentor_node=C

# External replication task.
ansible-playbook toolbox/tasks/set_mentor_node.yml -K -e target=1a -e db_name=Tenants \
    -e task_name=MyExternalRep -e task_type=external -e mentor_node=A
```

### `backup_database.yml`

```bash
# Default Logical backup to /backups/<db>-<timestamp>/.
ansible-playbook toolbox/backup/backup_database.yml -K -e target=1a -e db_name=Tenants

# Snapshot to a named path.
ansible-playbook toolbox/backup/backup_database.yml -K -e target=1a -e db_name=Tenants \
    -e backup_type=Snapshot -e backup_path=/backups/snap-test
```

### `restore_backup.yml`

```bash
# Restore on the same cluster (same shared /backups volume in docker mode).
# backup_path must point at the FOLDER containing the .ravendb-snapshot file, not the file itself.
ansible-playbook toolbox/backup/restore_backup.yml -K -e target=1a \
    -e backup_path=/backups/snap-test/<dated-folder> \
    -e new_db_name=Tenants_restored

# Cross-cluster restore (no docker cp needed -- shared volume).
ansible-playbook toolbox/backup/restore_backup.yml -K -e target=2a \
    -e backup_path=/backups/snap-test/<dated-folder> \
    -e new_db_name=Tenants_from_1a
```

### `open_subscription.yml`  (STUB)

```bash
# NOT IMPLEMENTED. Running it fails loudly with implementation guidance.
# See the file header for what to build (REST create/drop + Python consumer using ravendb client).
ansible-playbook toolbox/subscriptions/open_subscription.yml
```

---

## 3. Replication wiring (`toolbox/replication/`)

### `define_hub.yml`

```bash
# T3-style: one sink with a single prefix filter.
ansible-playbook toolbox/replication/define_hub.yml -K \
    -e hub_leader=1a -e db_name=Tenants -e hub_task_name=bidirectional-tenants \
    -e '{"sink_cluster_ids":[2], "sink_allowed_paths":{"2":["tenants/cluster2/*"]}}'

# T4-style: two sinks with disjoint filters.
ansible-playbook toolbox/replication/define_hub.yml -K \
    -e hub_leader=1a -e db_name=Tenants -e hub_task_name=hub-T4 \
    -e '{"sink_cluster_ids":[2,3],
         "sink_allowed_paths":{"2":["users/sink1/*","orders/sink1/*"],
                               "3":["users/sink2/*","orders/sink2/*"]}}'
```

### `attach_sinks.yml`

```bash
# Pairs with the T3-style define_hub above (hub_topology_urls must list every hub node).
ansible-playbook toolbox/replication/attach_sinks.yml -K \
    -e db_name=Tenants -e hub_task_name=bidirectional-tenants \
    -e '{"hub_topology_urls":["https://1a.hubsink.test:443","https://1b.hubsink.test:443","https://1c.hubsink.test:443"],
         "sink_cluster_ids":[2],
         "sink_allowed_paths":{"2":["tenants/cluster2/*"]}}'
```

---

## 4. End-to-end bring-up (paste-as-block)

```bash
# Spin up 2 clusters x 3 nodes, install RavenDB, form the clusters, create the DB on both,
# then wire hub-sink bidirectional pull-replication.
ansible-playbook playbooks/provision_nodes.yml -K -e clusters_count=2 -e nodes_per_cluster=3
ansible-playbook playbooks/install_ravendb.yml -K
ansible-playbook playbooks/form_clusters.yml -K
ansible-playbook toolbox/db/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants
ansible-playbook toolbox/db/create_database.yml -K -e cluster_leader=2a -e db_name=Tenants
ansible-playbook toolbox/replication/define_hub.yml -K \
    -e hub_leader=1a -e db_name=Tenants -e hub_task_name=bidirectional-tenants \
    -e '{"sink_cluster_ids":[2], "sink_allowed_paths":{"2":["tenants/cluster2/*"]}}'
ansible-playbook toolbox/replication/attach_sinks.yml -K \
    -e db_name=Tenants -e hub_task_name=bidirectional-tenants \
    -e '{"hub_topology_urls":["https://1a.hubsink.test:443","https://1b.hubsink.test:443","https://1c.hubsink.test:443"],
         "sink_cluster_ids":[2],
         "sink_allowed_paths":{"2":["tenants/cluster2/*"]}}'
```

RESET (heal everything + drop+recreate DBs + rewire replication; one-liner via `scripts/reset_hub_sink.sh`):

```bash
./scripts/reset_hub_sink.sh
```

---

## 5. EMR scenarios (`scenarios/EMR/`)

### RV-1 -- mid-rolling-upgrade leader restart + asymmetric partition

End-to-end wrapper (teardown → provision @ v_old → run scenario):

```bash
# build the v_new .deb (once per PR)
scripts/build_ravendb_pr.sh 22875
# → builds/raven-pr22875.deb

# run the full RV-1 test
scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb
```

Or run the scenario directly against an already-provisioned cluster at v_old:

```bash
ansible-playbook scenarios/EMR/RV1/rv1.yml \
    -e v_old=6.2.15 \
    -e v_new_build=$PWD/builds/raven-pr22875.deb

# full 50k-revision spec
ansible-playbook scenarios/EMR/RV1/rv1.yml \
    -e v_old=6.2.15 -e v_new_build=$PWD/builds/raven-pr22875.deb \
    -e pool_size=10000
```

---

## 6. SSH-mode (VMs / bare metal / other computers)

Same harness, different inventory. Every command becomes `ansible-playbook -i inventory/ssh_hosts.yml ...` and node names map to entries in that file. No other differences - the toolbox + scenarios behave the same.

### Bring-up

```bash
# Copy the inventory template and fill in hosts.
cp inventory/ssh_hosts.yml.example inventory/ssh_hosts.yml

# Verify SSH access + install apt prereqs on each host.
ansible-playbook -i inventory/ssh_hosts.yml playbooks/setup_ssh_targets.yml

# Install RavenDB on every host (arm64 deb auto-selected on aarch64).
ansible-playbook -i inventory/ssh_hosts.yml playbooks/install_ravendb.yml

# Merge them into a cluster + write /etc/hosts everywhere.
ansible-playbook -i inventory/ssh_hosts.yml playbooks/form_clusters.yml
```

### Toolbox in ssh mode (just prepend the inventory flag)

```bash
# Cut a link between two SSH hosts.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/network/cut_link.yml -e node_a=1a -e node_b=1b

# Partition one host from its peers.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/network/partition_node.yml -e target=1c

# Restart RavenDB on a host (systemctl over SSH, then wait for HTTPS).
ansible-playbook -i inventory/ssh_hosts.yml toolbox/service/restart_ravendb.yml -e target=1a

# Hard-delete a database (REST API + stop/rm-rf/start on each peer over SSH).
ansible-playbook -i inventory/ssh_hosts.yml toolbox/db/delete_database.yml -e cluster_leader=1a -e db_name=Tenants

# Upgrade one host to a specific version.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/service/upgrade_node.yml -e target=1a -e rdb_version=7.2.3
```

### Teardown

```bash
ansible-playbook -i inventory/ssh_hosts.yml playbooks/cleanup_ssh_targets.yml
```
