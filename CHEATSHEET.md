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

### `wait_for_etag_parity.yml`

```bash
# Per-node LastDatabaseEtag stability -- two reads ~3s apart, every node identical = drained.
# spec-aligned "wait by etag" -- replaces wait_for_quiescence for post-workload settles.
ansible-playbook toolbox/wait/wait_for_etag_parity.yml \
    -e db_name=db1 -e '{"nodes":["1a","1b","1c"]}'

# Longer budget for slower convergence
ansible-playbook toolbox/wait/wait_for_etag_parity.yml \
    -e db_name=db1 -e '{"nodes":["1a","1b","1c"]}' -e timeout=600
```

### `wait_for_stats_field_parity.yml`

```bash
# Default -- wait until CountOfTombstones matches across the sink cluster (5min ceiling)
ansible-playbook toolbox/wait/wait_for_stats_field_parity.yml \
    -e db_name=db1 -e '{"nodes":["2a","2b","2c"]}'

# Multi-field wait
ansible-playbook toolbox/wait/wait_for_stats_field_parity.yml \
    -e db_name=db1 \
    -e '{"nodes":["2a","2b","2c"],"fields":["CountOfTombstones","CountOfDocuments"]}' \
    -e timeout=300
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

### `assert_workload_alive.yml`

```bash
# Use BEFORE stop_workload -- catches silently-died workers (OOM, crash, etc.) instead of
# letting stop_workload shrug "WORKLOAD ALREADY EXITED" and pass with degraded load.
ansible-playbook toolbox/workloads/assert_workload_alive.yml \
    -e pidfile=/tmp/w1-1a-db1.pid
```

### `pause_gate.yml`  (toolbox/control) -- kept as a standalone example, not used in scenarios

```bash
# Prints a section banner; pauses for ENTER when pause_between_sections=true.  Active
# scenarios don't import it -- they're meant for unattended PR runs.  This wrapper stays
# around as a building block for future demo / showcase playbooks.
ansible-playbook toolbox/control/pause_gate.yml \
    -e section_label="SECTION 5 -- upgrade 1a" \
    -e pause_between_sections=true
```

### `smuggler_import.yml`  (toolbox/backup)

```bash
# Stream a .ravendbdump file from controller into an existing DB.  Used by RP-1 to seed a
# legacy-format counter from a v_old smuggler dump fixture.
ansible-playbook toolbox/backup/smuggler_import.yml -K \
    -e target=1a -e db_name=db1 \
    -e dump_path=$PWD/scenarios/company-1/RP1/fixtures/legacy-counter.ravendbdump

# No-op if fixture file isn't present (useful for optional fixtures)
ansible-playbook toolbox/backup/smuggler_import.yml -K \
    -e target=1a -e db_name=db1 \
    -e dump_path=$PWD/scenarios/company-1/RP1/fixtures/legacy-counter.ravendbdump \
    -e skip_if_missing=true
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

### `diagnostic_orphan_revisions.yml`

```bash
# Trigger adopt on every node + assert AdoptedCount==0 everywhere.
ansible-playbook toolbox/diagnostic/diagnostic_orphan_revisions.yml \
    -e db_name=db1 -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_extension_stats_parity.yml`

```bash
# All three extension aspects (attachments + counters + timeseries).
ansible-playbook toolbox/diagnostic/diagnostic_extension_stats_parity.yml \
    -e db_name=db1 -e '{"nodes":["1a","1b","1c"]}'

# Only the aspect you care about.
ansible-playbook toolbox/diagnostic/diagnostic_extension_stats_parity.yml \
    -e db_name=db1 -e aspects=attachments \
    -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_filter_compliance.yml`

```bash
# Explicit allowed prefixes (recommended -- bypasses DatabaseRecord parse).
ansible-playbook toolbox/diagnostic/diagnostic_filter_compliance.yml \
    -e sink_cluster_leader=2a -e db_name=db1 \
    -e '{"allowed_prefixes":["users/sink1/"]}'

# Auto-fetch from DatabaseRecord.SinkPullReplications[*].AllowedHubToSinkPaths.
ansible-playbook toolbox/diagnostic/diagnostic_filter_compliance.yml \
    -e sink_cluster_leader=2a -e db_name=db1
```

### `diagnostic_stored_item_cv_split.yml`

```bash
# Post-upgrade: every probed doc on the v_new receiver must be in the new split-form CV.
ansible-playbook toolbox/diagnostic/diagnostic_stored_item_cv_split.yml \
    -e db_name=db1 -e target=2a \
    -e '{"doc_ids":["users/sink1/0","users/sink1/1","users/sink1/2"]}'

# Pre-upgrade baseline: every probed doc must be in the OLD raw-CV form.
ansible-playbook toolbox/diagnostic/diagnostic_stored_item_cv_split.yml \
    -e db_name=db1 -e target=1a -e expect=raw \
    -e '{"doc_ids":["users/hub/0","Internal/0"]}'
```

### `diagnostic_stats_parity.yml`

```bash
# Consolidated /stats parity across nodes (12 fields, 1 GET/node) -- replaces
# doc_count_parity + extension_stats_parity chain.  Prints a single table; asserts 9 of 11 Count*
# fields (CountOfCounterEntries + CountOfTimeSeriesSegments + SizeOnDisk are info-only).
ansible-playbook toolbox/diagnostic/diagnostic_stats_parity.yml \
    -e db_name=db1 -e '{"nodes":["1a","1b","1c"]}'

# Subset assert (only docs + tombstones)
ansible-playbook toolbox/diagnostic/diagnostic_stats_parity.yml \
    -e db_name=db1 \
    -e '{"nodes":["1a","1b","1c"],"assert_fields":["CountOfDocuments","CountOfTombstones"]}'

# Information-only (no asserts) -- safe under live writes
ansible-playbook toolbox/diagnostic/diagnostic_stats_parity.yml \
    -e db_name=db1 -e informational_only=true \
    -e '{"nodes":["1a","1b","1c"]}'
```

### `diagnostic_cv_boundary_by_dbid.yml`

```bash
# I-13 (b) by DatabaseId -- works in T3 where tag-letter is too lax (every cluster has A/B/C).
# Default: tool reports N/A on legacy CV form (no '|' delimiter), informational.
ansible-playbook toolbox/diagnostic/diagnostic_cv_boundary_by_dbid.yml \
    -e db_name=db1 \
    -e '{"source_nodes":["1a","1b","1c"],"receiver_nodes":["2a","2b","2c"]}'

# Strict mode: FAIL if any receiver is in legacy form (i.e. new lane hasn't activated)
ansible-playbook toolbox/diagnostic/diagnostic_cv_boundary_by_dbid.yml \
    -e db_name=db1 -e strict_v_new=true \
    -e '{"source_nodes":["1a","1b","1c"],"receiver_nodes":["2a","2b","2c"]}'
```

### `diagnostic_lane_inert.yml`

```bash
# Assert no v_new-lane CV ('|' delimiter) appears in any sampled revision across the probed
# nodes/prefixes.  Use mid-roll on cross-version connections.
ansible-playbook toolbox/diagnostic/diagnostic_lane_inert.yml \
    -e db_name=db1 \
    -e '{"nodes":["2a","2b","2c"]}' \
    -e '{"id_prefixes":["users/sink1","orders/sink1"]}'
```

### `diagnostic_cross_sink_isolation.yml`

```bash
# Probe sibling-sink ids on a sink leader; fails on any 200 (data leaked across).
ansible-playbook toolbox/diagnostic/diagnostic_cross_sink_isolation.yml \
    -e db_name=db1 -e sink_cluster_leader=2a \
    -e '{"forbidden_prefixes":["users/sink2/","orders/sink2/"]}'
```

### `diagnostic_db_cv_order_side_only.yml`

```bash
# Assert the receiver-cluster's DatabaseChangeVector references only receiver-side tags
# (A, B, C derived from 2a/2b/2c).  Catches source-side tag leakage into a sink's DB CV.
ansible-playbook toolbox/diagnostic/diagnostic_db_cv_order_side_only.yml \
    -e db_name=db1 -e '{"receiver_group_nodes":["2a","2b","2c"]}'
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

## 4.5. company-1 workloads (`scenarios/company-1/workloads/`)

Continuous background workloads driven by the company-1 scenarios.  All accept a pidfile-based start/stop contract — pair with `toolbox/workloads/wait_for_workload_started.yml` and `stop_workload.yml`.

### `workload_w1.yml` -- doc CRUD churn (70% update / 20% put-new / 10% delete)

```bash
# Single-bucket (legacy mode)
ansible-playbook scenarios/company-1/workloads/w1/workload_w1.yml \
    -e target=1a -e db_name=db1 -e id_prefix=seed -e pool_size=10000 -e duration_secs=300

# Multi-bucket (RPV-1 mode) -- weighted prefix:pool_size:weight triples
ansible-playbook scenarios/company-1/workloads/w1/workload_w1.yml \
    -e target=1a -e db_name=db1 -e duration_secs=600 \
    -e 'buckets_spec=users/sink1:2000:13|orders/sink1:2000:13|users/hub:2000:14|Internal:3000:20'

# Multi-writer parallel (RV-1's 4-writer pattern) -- use WRITER_ID for disjoint pidfiles
nohup ansible-playbook scenarios/company-1/workloads/w1/workload_w1.yml \
    -e target=1a -e db_name=db1 -e id_prefix=seed -e pool_size=200 -e writer_id=1 &
nohup ansible-playbook scenarios/company-1/workloads/w1/workload_w1.yml \
    -e target=1a -e db_name=db1 -e id_prefix=seed -e pool_size=200 -e writer_id=2 &
# ... etc

# Indefinite mode -- omit duration_secs, kill via stop_workload
ansible-playbook scenarios/company-1/workloads/w1/workload_w1.yml \
    -e target=1a -e db_name=db1 -e id_prefix=seed -e pool_size=200
```

### `workload_w2.yml` -- doc-extension churn (attachments / counters / time-series)

```bash
# Same shape as W-1 -- single-bucket or multi-bucket.  NO doc CRUD (that's W-1's job).
ansible-playbook scenarios/company-1/workloads/w2/workload_w2.yml \
    -e target=1a -e db_name=db1 -e duration_secs=600 \
    -e 'buckets_spec=users/sink1:2000:13|orders/sink1:2000:13|users/hub:2000:14|Internal:3000:20'
```

### `workload_w3.yml` -- concurrent churn races on a hot pool (RV-1 Phase 2)

```bash
# N workers (default 8) racing delete -> revert-from-revision -> put -> +att -> -att.
# Requires `jq` on the controller.
ansible-playbook scenarios/company-1/workloads/w3/workload_w3.yml \
    -e target=1a -e db_name=db1 -e duration_secs=300 \
    -e id_prefix=hot -e pool_size=1000

# Custom writer count
ansible-playbook scenarios/company-1/workloads/w3/workload_w3.yml \
    -e target=1a -e db_name=db1 -e duration_secs=300 \
    -e id_prefix=hot -e pool_size=1000 -e writers=4
```

### `workload_w4.yml` -- filter-boundary churn (50/50 in-prefix / out-prefix writes)

```bash
# Hammer the filter boundary by interleaving writes to an in-allowed prefix vs an out-allowed
# prefix.  Used by RP-2 step 6 (widen + narrow with backlog under W-4 load).
ansible-playbook scenarios/company-1/workloads/w4/workload_w4.yml \
    -e target=1a -e db_name=db1 \
    -e in_prefix=users -e out_prefix=orders \
    -e pool_size=1000 -e duration_secs=60
```

### `workload_w5.yml` -- conflict generator (concurrent writes on the same doc-id pool)

```bash
# Launch TWO instances on opposite sides of a partition with same id_prefix + pool_size and
# DISTINCT side_label so the resulting writes conflict on heal.  Used by RP-2 step 20.
ansible-playbook scenarios/company-1/workloads/w5/workload_w5.yml \
    -e target=1a -e db_name=db1 \
    -e id_prefix=users/conflict -e pool_size=500 \
    -e side_label=majority -e duration_secs=180
```

### `workload_w7.yml` -- single-doc revision firehose (RV-1 Phase 3)

```bash
# One curl PUT loop targeting users/hot for the configured duration.  spec target:
# 16k revs/min for 60 min -> 1M revisions.  Actual rate is whatever a single writer
# can sustain; final PUT count printed at exit.
ansible-playbook scenarios/company-1/workloads/w7/workload_w7.yml \
    -e target=1a -e db_name=db1 -e doc_id=users/hot -e duration_secs=3600
```

### `workload_w7_reader.yml` -- concurrent reader for W-7

```bash
# Every 30s GET /revisions?id=users/hot (full history); every 5min snapshot SizeOnDisk +
# CountOfRevisionDocuments.  Log lands at /tmp/w7-reader-1a-db1.log.
ansible-playbook scenarios/company-1/workloads/w7/workload_w7_reader.yml \
    -e target=1a -e db_name=db1 -e doc_id=users/hot -e duration_secs=3600
```

---

## 4.6. Calling the `ravendb_*` Python modules directly (`library/`)

Six custom modules under `library/` are the canonical way to call RavenDB from inside a
scenario's `tasks:` block.  The toolbox YAML wrappers remain as CLI examples but scenarios
should prefer direct module calls -- one task instead of an `import_playbook` round-trip,
results in `register: r` for inline assertions, no var-scoping gymnastics.

Every call requires `ravendb_domain`, `client_cert`, `ca_cert` (auto-resolvable from
`group_vars/all.yml`).  Most kinds take `target` + `db_name`; parity / sweep kinds take
`nodes` instead.  Set `assert_mode: true` to make a parity check fail loud (default is
info-only output).

### `ravendb_revisions` -- configure document revisions

```yaml
# Default config -- minimum N revisions kept, optional age purge
- ravendb_revisions:
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    target: 1a
    db_name: db1
    default_keep: 25
    default_max_age_secs: 21600

# Per-collection config
- ravendb_revisions:
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    target: 1a
    db_name: db1
    collections_config:
      Users:    { keep: 10, max_age_secs: 1800 }
      Orders:   { keep: 50, max_age_secs: 7200 }
      Internal: { keep: 25, max_age_secs: 21600 }

# Keep-all (no purge -- RV-1 phase 3 uses this for the 1M-rev firehose)
- ravendb_revisions:
    target: 1a
    db_name: db1
    minimum_revisions: 100000000
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
```

### `ravendb_writes` -- writes (9 `kind:`s)

```yaml
# kind: docs                -- N PUTs with the same body (single revision per id; dedupes)
# kind: docs_freeform       -- N PUTs with random GUID ids
# kind: docs_revisions      -- N docs x M revisions, DISTINCT body per PUT
# kind: docs_interleaved    -- round-robin across multiple prefixes
# kind: attachments         -- add attachment to N existing docs
# kind: counters            -- increment a counter N times on one doc
# kind: timeseries          -- append (or delete a range) on one doc's TS
# kind: delete              -- DELETE by id list OR by prefix+count
# kind: restore_revision    -- restore an older revision as the new live doc

- ravendb_writes:
    kind: docs_revisions
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    target: 1a
    db_name: db1
    id_prefix: seed
    collection: MicroDocs
    count: 10000
    revs_per_doc: 5
  register: r
- debug: { msg: "{{ r.msg }}" }
```

### `ravendb_wait` -- sync gates (9 `kind:`s)

```yaml
# kind: leader              -- /cluster/topology reports a Leader (optionally pinned)
# kind: member              -- target node is full Member of cluster
# kind: rehab               -- target node entered DB-level rehab
# kind: etag_parity         -- per-node LastDatabaseEtag stable across 2 samples (drained)
# kind: docs_drain          -- per-node CV stable across 2 samples
# kind: quiescence          -- DatabaseChangeVector equality cross-node (converged)
# kind: stats_field_parity  -- /stats fields equal cross-node (post-cleanup tombstone wait)
# kind: conflicts_resolved  -- /replication/conflicts == 0 on every node
# kind: marker_propagation  -- PUT marker on source, poll until present on every target

- ravendb_wait:
    kind: etag_parity
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    db_name: db1
    nodes: [1a, 1b, 1c]
    timeout: 180
  register: r
- debug: { msg: "{{ r.msg }}" }
```

### `ravendb_tasks` -- ongoing-task ops (4 `kind:`s)

```yaml
# kind: define_hub          -- hub-side: define PullReplicationAsHub + per-sink access entries
# kind: attach_sinks        -- sink-side: create connection string + sink-pull task w/ PFX
# kind: mutate_sink_filter  -- live-mutate AllowedHubToSinkPaths on an existing sink-pull task
# kind: set_mentor_node     -- flip MentorNode on hub / sink / external replication task

- ravendb_tasks:
    kind: define_hub
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    hub_leader: 1a
    db_name: db1
    hub_task_name: rp1-hub
    sink_cluster_ids: [2]
    sink_allowed_paths: { "2": ["users/sink1/*"] }
    replication_certs_dir: "{{ playbook_dir }}/../../../replication-certs"

- ravendb_tasks:
    kind: set_mentor_node
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    target: 1a
    db_name: db1
    task_name: rp2-hub-to-sink1
    task_type: hub        # or sink / external
    mentor_node: B
```

### `ravendb_backup` -- backup / restore / smuggler (3 `kind:`s)

```yaml
# kind: backup              -- trigger Logical/Snapshot backup, wait for completion
# kind: restore             -- restore a backup folder as a new DB, wait for completion
# kind: smuggler_import     -- POST a .ravendbdump from controller into an existing DB

- ravendb_backup:
    kind: backup
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    target: 1a
    db_name: db1
    backup_type: Backup      # or Snapshot
    backup_path: /backups/run1
```

### `ravendb_diagnostic` -- read-only checks (19 `kind:`s)

Info-only by default; pass `assert_mode: true` to make a parity / invariant kind fail loud.

```yaml
# info-only kinds:
#   doc_count, replication, schema_version, size_envelope,
#   capture_cv, capture_doc_cv, scan_fltr
# parity / invariant kinds (accept assert_mode):
#   doc_count_parity, doc_id_set_parity, revision_count_parity, orphan_revisions,
#   extension_stats_parity, stored_item_cv_split, lane_inert, filter_compliance,
#   cross_sink_isolation, cv_boundary_by_dbid, db_cv_order_side_only, stats_parity

- ravendb_diagnostic:
    kind: stats_parity
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    db_name: db1
    nodes: [1a, 1b, 1c]
    assert_mode: true
  register: r
- debug: { msg: "{{ r.msg }}" }

- ravendb_diagnostic:
    kind: cv_boundary_by_dbid
    ravendb_domain: "{{ ravendb_domain }}"
    client_cert: "{{ cert_dir }}/client.pem"
    ca_cert: "{{ cert_ca_crt }}"
    db_name: db1
    source_nodes: [1a, 1b, 1c]
    receiver_nodes: [2a, 2b, 2c]
    assert_mode: true
```

---

## 5. company-1 scenarios (`scenarios/company-1/`)

Active scenarios: **RV-1**, **RP-1**, **RP-2**, **RPV-1** — each implements one slice of [`company-1_TESTING_PLAN/scenarios.md`](company-1_TESTING_PLAN/scenarios.md).

Common conventions:
* Each scenario has a `run.sh` wrapper: `teardown → provision → install → form_clusters → scenario`.
* `run.sh <args...> [cid] [net] [-e foo=bar ...]` — positional args stop at the first `-flag`, rest go to the scenario as overrides.
* Smoke-size any scenario via `-e` overrides (see the smoke-mode subsection per scenario below).

### RV-1 -- single-cluster `v_62 → v_new` full chain + churn + 1M-rev (T1)

Single 3-node cluster.  Phase 1 rolling upgrade under W-1×4.  Phase 2 W-3 8-writer churn.
Phase 3 W-7 1M-revision firehose on `users/hot` + concurrent reader.  Phase 4 count-parity
sweep across the union dataset.

```bash
# End-to-end (~90 min full spec sizing)
scenarios/company-1/RV1/run.sh 6.2.15 builds/raven-pr22875.deb

# Shakedown (~5 min) -- lab already up
ansible-playbook scenarios/company-1/RV1/rv1.yml -K \
    -e v_old=6.2.15 -e v_new_build=$PWD/builds/raven-pr22875.deb \
    -e phase1_seed_count=200 -e phase1_seed_revs_per_doc=2 \
    -e phase2_hot_count=20 -e phase2_hot_revs_per_doc=3 -e phase2_churn_duration_secs=30 \
    -e phase3_duration_secs=60 \
    -e upgrade_delay_secs=0 \
    2>&1 | tee logs/rv1-smoke-$(date +%Y%m%d-%H%M%S).log
```

### RP-1 -- CV-boundary regression guard on a clean all-v_new T2 cluster

1 hub + 1 filtered-pull sink (RF=3 each).  W-0 deterministic bulk seed + per-family inventory
(one item per replication-item type, **including a legacy-format counter from a smuggler dump
fixture** — see [`scenarios/company-1/RP1/fixtures/README.md`](scenarios/company-1/RP1/fixtures/README.md)).
Burst: 10k `users/sink1/active/*` writes + 2k delete/restore-from-revision iterations.
Asserts I-13 (a)/(b)/(c) + I-7 + I-5/I-6.  spec step 8: local update on `sink/b`, verify
replicates to `a/c`.

```bash
# End-to-end (~12 min spec sizing)
scenarios/company-1/RP1/run.sh builds/raven-pr22875.deb

# Shakedown (~5 min)
ansible-playbook scenarios/company-1/RP1/rp1.yml -K \
    -e bulk_users_sink1=100 -e bulk_orders_hub=100 \
    -e burst_active_count=100 -e burst_restore_iterations=50 \
    -e drain_budget_secs=60 -e filter_drain_budget_secs=180 \
    2>&1 | tee logs/rp1-smoke-$(date +%Y%m%d-%H%M%S).log
```

### RP-2 -- compound replication chaos across hub + 2 sinks (T3, all v_new)

Hub + 2 sinks (RF=3 each), 9 nodes all on v_new throughout (no upgrade).  4 phases:
phase (a) echo prevention + concurrent conflict + cross-sink leak, phase (b) 7 filter mutation
sub-steps, phase (c) mentor rotation + hard kill + node remove/rejoin, phase (d) split-brain.
Currently blocked at step 6b on a known RavenDB filter-mutation live-propagation gap (pending
upstream reply — the test correctly surfaces the issue).

```bash
# End-to-end (~40 min spec sizing)
scenarios/company-1/RP2/run.sh builds/raven-pr22875.deb

# Shakedown (~10 min)
scenarios/company-1/RP2/run.sh builds/raven-pr22875.deb \
    -e setup_hub_users=200 -e setup_sink1_users=200 \
    -e phase_a_active_count=100 -e phase_a_conflict_count=100 -e phase_a_leak_check_count=100 \
    -e phase_b_backlog_count=100 -e phase_b_archived_count=200 \
    -e phase_b_w4_duration_secs=15 -e phase_b_w1_duration_secs=20 \
    -e phase_b_partition_window_secs=15 -e phase_b_egress_window_secs=15 \
    -e phase_b_mentor_restart_count=2 -e phase_b_mentor_restart_window_secs=30 \
    -e phase_b_retry_storm_window_secs=30 -e phase_b_retry_storm_period_secs=10 \
    -e phase_c_mentor_rotation_window_secs=30 -e phase_c_w1_post_kill_secs=20 \
    -e phase_c_remove_node_wait_secs=20 \
    -e phase_d_w5_window_secs=30 -e phase_d_w5_pool_size=100 \
    -e parity_probe_count=10 -e burst_cooldown_secs=5 \
    2>&1 | tee logs/rp2-smoke-$(date +%Y%m%d-%H%M%S).log
```

### RPV-1 -- cross-cluster `v_62 → v_new` rolling upgrade, filter-aware (T3)

Hub + Sink-1 (filter `users/sink1/* + orders/sink1/*`) + Sink-2 (filter `users/sink2/* +
orders/sink2/*`).  All 9 nodes start at v_old.  Seed Hub with 7 prefix buckets, run W-1 + W-2
on Hub indefinitely (stopped explicitly by Section 12), roll all 9 nodes to v_new in a
variant-specific order, with checkpoints between every 3-node upgrade step.

```bash
# End-to-end variant A (~30 min spec sizing)
scenarios/company-1/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb

# Shakedown variant A (~10 min)
ansible-playbook scenarios/company-1/RPV1/rpv1.yml -K \
    -e v_old=6.2.15 -e v_new_build=$PWD/builds/raven-pr22875.deb \
    -e bucket_users_sink1=100 -e bucket_users_sink2=100 -e bucket_users_hub=100 \
    -e bucket_orders_sink1=100 -e bucket_orders_sink2=100 -e bucket_orders_hub=100 \
    -e bucket_internal=200 \
    -e quiesce_budget_secs=300 -e drain_budget_secs=60 -e filter_drain_budget_secs=180 \
    2>&1 | tee logs/rpv1-smoke-A-$(date +%Y%m%d-%H%M%S).log
```

**Variants.**  Default is A (sinks first).  Override the three upgrade-step lists on the CLI:

```bash
# Variant B -- hub first (stresses v_new sender -> v_old receivers at Checkpoint A)
ansible-playbook scenarios/company-1/RPV1/rpv1.yml -K \
    -e v_old=6.2.15 -e v_new_build=$PWD/builds/raven-pr22875.deb \
    -e '{"upgrade_step_1":["1a","1b","1c"],
         "upgrade_step_2":["2a","2b","2c"],
         "upgrade_step_3":["3a","3b","3c"]}'

# Variant C -- interleaved random shuffle
ansible-playbook scenarios/company-1/RPV1/rpv1.yml -K \
    -e v_old=6.2.15 -e v_new_build=$PWD/builds/raven-pr22875.deb \
    -e '{"upgrade_step_1":["2a","1b","3c"],
         "upgrade_step_2":["1a","3b","2c"],
         "upgrade_step_3":["3a","1c","2b"]}'
```

### Running scenarios in parallel on the same docker host

Each `run.sh` accepts `[cid] [net]` positional args.  Each parallel run must use a **disjoint
cluster_id range** + **unique network name**.  Container names are globally unique per docker
daemon -- a cid collision will stomp the other run's containers.

| Scenario | cids needed | suggested `cid` | suggested `net` |
|---|---|---|---|
| RV-1   | 1 | `1` | `rv1net`   |
| RP-1   | 2 | `2` | `rp1net`   |
| RP-2   | 3 | `4` | `rp2net`   |
| RPV-1  | 3 | `7` | `rpv1net`  |

Export the BECOME password once so no shell re-prompts:

```bash
read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS

# Shell A
scenarios/company-1/RV1/run.sh 6.2.15 builds/raven-pr22875.deb 1 rv1net   ...  2>&1 | tee logs/rv1-$(date +%Y%m%d-%H%M).log
# Shell B
scenarios/company-1/RP1/run.sh builds/raven-pr22875.deb 2 rp1net          ...  2>&1 | tee logs/rp1-$(date +%Y%m%d-%H%M).log
# Shell C
scenarios/company-1/RP2/run.sh builds/raven-pr22875.deb 4 rp2net          ...  2>&1 | tee logs/rp2-$(date +%Y%m%d-%H%M).log
# Shell D
scenarios/company-1/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb 7 rpv1net ...  2>&1 | tee logs/rpv1-$(date +%Y%m%d-%H%M).log
```

All four in parallel = 27 RavenDB containers.  WSL2 default kernel limits (`pid_max=32768`)
can hit `OCI runtime exec failed: setns process: exit status 1` under that load -- see
[NOTES.md § WSL2 kernel limits](NOTES.md) for the one-time `sysctl` bump.

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
