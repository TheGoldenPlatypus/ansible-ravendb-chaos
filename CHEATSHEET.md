# Cheatsheet

Copy-paste commands to exercise every playbook in the repo, including each optional-var variant.
Run from the repo root (`ansible-ravendb-chaos/`).

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
ansible-playbook playbooks/install_ravendb.yml -K -e rdb_version=6.2.6

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
# Remove every container on the docker network, drop the network, strip /etc/hosts.
ansible-playbook playbooks/teardown_containers.yml -K
```

---

## 2. Toolbox (`toolbox/`)

### `cut_link.yml`

```bash
# Cut all TCP between two containers (forces TCP reset).
ansible-playbook toolbox/cut_link.yml -K -e node_a=1a -e node_b=1b
```

### `restore_link.yml`

```bash
# Undo a previous cut_link between two containers.
ansible-playbook toolbox/restore_link.yml -K -e node_a=1a -e node_b=1b
```

### `partition_node.yml`

```bash
# Cut one node off from every cluster peer in one go.
ansible-playbook toolbox/partition_node.yml -K -e target=1c
```

### `heal_node.yml`

```bash
# Undo a previous partition_node by restoring every peer link.
ansible-playbook toolbox/heal_node.yml -K -e target=1c
```

### `restart_ravendb.yml`

```bash
# Restart the RavenDB service inside one container and wait for HTTPS.
ansible-playbook toolbox/restart_ravendb.yml -K -e target=1b

# Same, with a custom wait budget (seconds).
ansible-playbook toolbox/restart_ravendb.yml -K -e target=1b -e timeout_secs=240
```

### `write_docs.yml`

```bash
# Write 50 docs with the default id_prefix (micro/doc/0..49).
ansible-playbook toolbox/write_docs.yml -K -e target=1a -e db_name=Tenants -e count=50

# Write 20 docs with a custom id prefix.
ansible-playbook toolbox/write_docs.yml -K -e target=1a -e db_name=Tenants -e count=20 -e id_prefix=probe/burst1
```

### `write_docs_interleaved.yml`

```bash
# Round-robin write across two prefixes (A-0, B-0, A-1, B-1, ...).
ansible-playbook toolbox/write_docs_interleaved.yml -K -e target=1a -e db_name=Tenants -e count=20 -e '{"prefixes":["tenants/cluster2/x","other/x"]}'

# Round-robin across three prefixes (X-0, Y-0, Z-0, X-1, ...).
ansible-playbook toolbox/write_docs_interleaved.yml -K -e target=1a -e db_name=Tenants -e count=15 -e '{"prefixes":["X","Y","Z"]}'
```

### `read_doc_count.yml`

```bash
# Print CountOfDocuments from /stats on a target node.
ansible-playbook toolbox/read_doc_count.yml -K -e target=1b -e db_name=Tenants
```

### `create_database.yml`

```bash
# Create the DB on every node in the cluster (default replication_factor=3).
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants

# Create a single-node DB (replication_factor=1).
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=1a -e db_name=MyTestDb -e replication_factor=1
```

### `delete_database.yml`

```bash
# Delete a DB and poll until it's gone (default 60s wait budget).
ansible-playbook toolbox/delete_database.yml -K -e cluster_leader=1a -e db_name=Tenants

# Same, with a longer wait budget.
ansible-playbook toolbox/delete_database.yml -K -e cluster_leader=1a -e db_name=Tenants -e timeout_secs=180
```

### `remove_node.yml`

```bash
# Remove the node with cluster tag D from cluster 1.
ansible-playbook toolbox/remove_node.yml -K -e cluster_leader=1a -e target_tag=D
```

### `upgrade_node.yml`

```bash
# Upgrade one node to a specific version from daily-builds.
ansible-playbook toolbox/upgrade_node.yml -K -e target=1a -e rdb_version=7.2.3

# Upgrade one node from a custom .deb (dev branch artifact).
ansible-playbook toolbox/upgrade_node.yml -K -e target=1a -e custom_build=/tmp/raven-branch.deb --skip-tags download

# Upgrade with a longer wait budget for the node to come back.
ansible-playbook toolbox/upgrade_node.yml -K -e target=1a -e rdb_version=7.2.3 -e timeout_secs=300

# Rolling upgrade across a 3-node cluster, with a health gate between each step.
for n in 1a 1b 1c; do ansible-playbook toolbox/upgrade_node.yml -K -e target=$n -e rdb_version=7.2.3; ansible-playbook toolbox/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity; done
```

### `show_replication.yml`

```bash
# Dump incoming + outgoing replication connections for the DB on a node.
ansible-playbook toolbox/show_replication.yml -K -e target=2a -e db_name=Tenants
```

### `wait_for_healthy.yml`

```bash
# Cheap recovery check (entry-point node responds + members can talk).
ansible-playbook toolbox/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity

# Same, with a longer total wait budget.
ansible-playbook toolbox/wait_for_healthy.yml -K -e cluster_leader=1a -e checks=node_alive,cluster_connectivity -e max_wait=180
```

### `wait_for_rehab.yml`

```bash
# Block until 1b enters Promotables/Rehabs on Tenants's topology (fails if it doesn't within 120s).
ansible-playbook toolbox/wait_for_rehab.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b

# Longer wait budget.
ansible-playbook toolbox/wait_for_rehab.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b -e timeout_secs=300
```

### `wait_for_member.yml`

```bash
# Block until 1b is back as a full Member of Tenants's topology (default 300s budget).
ansible-playbook toolbox/wait_for_member.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b

# Longer wait budget for slow rehab phases.
ansible-playbook toolbox/wait_for_member.yml -K -e cluster_leader=1a -e db_name=Tenants -e target=1b -e timeout_secs=600
```

### `write_docs_freeform.yml`

```bash
# Write 1 freeform doc to 2a (random GUID id, null collection).
ansible-playbook toolbox/write_docs_freeform.yml -K -e target=2a -e db_name=Tenants -e count=1

# Write 5 freeform docs to 2b.
ansible-playbook toolbox/write_docs_freeform.yml -K -e target=2b -e db_name=Tenants -e count=5
```

### `delete_docs.yml`

```bash
# Delete 50 docs that match the default write_docs.yml prefix.
ansible-playbook toolbox/delete_docs.yml -K -e target=1a -e db_name=Tenants -e id_prefix=micro/doc -e count=50

# Delete an explicit list of ids.
ansible-playbook toolbox/delete_docs.yml -K -e target=1a -e db_name=Tenants -e '{"ids":["users/1","users/42"]}'
```

### `force_cluster_asymmetry.yml`

```bash
# Asymmetric hub: 1b on 7.2.3, 1c on a dev .deb, 1a untouched.
ansible-playbook toolbox/force_cluster_asymmetry.yml -K -e '{"version_map":{"1b":"7.2.3","1c":"/tmp/raven-feature-branch.deb"}}'

# Whole hub cluster to the same new version (regular rolling upgrade in one shot).
ansible-playbook toolbox/force_cluster_asymmetry.yml -K -e '{"version_map":{"1a":"7.2.3","1b":"7.2.3","1c":"7.2.3"}}'

# Hub upgraded, sink left on old version -- cross-version replication test.
ansible-playbook toolbox/force_cluster_asymmetry.yml -K -e '{"version_map":{"1a":"7.2.3","1b":"7.2.3","1c":"7.2.3"}}'
```

---

## 3. Scenarios (`scenarios/hub-sink/`)

### `tasks/define_hub.yml`

```bash
# Hub-side setup: define pull-replication task + mint per-sink certs + register hub access entries.
ansible-playbook scenarios/hub-sink/tasks/define_hub.yml -K -e clusters_count=2
```

### `tasks/attach_sinks.yml`

```bash
# Sink-side setup: create the connection string + sink-pull task on every sink.
ansible-playbook scenarios/hub-sink/tasks/attach_sinks.yml -K -e clusters_count=2
```

### `chaos_failover.yml`

```bash
# Full hub-sink chaos failover (needs hub+sink cluster already wired). Interactive (pauses for Enter).
ansible-playbook scenarios/hub-sink/chaos_failover.yml -K
```

---

## 4. End-to-end bring-up (paste-as-block)

```bash
# Spin up 2 clusters x 3 nodes, install RavenDB, form the clusters, create the DB on both,
# then wire hub-sink bidirectional pull-replication.
ansible-playbook playbooks/provision_nodes.yml -K -e clusters_count=2 -e nodes_per_cluster=3
ansible-playbook playbooks/install_ravendb.yml -K
ansible-playbook playbooks/form_clusters.yml -K
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=2a -e db_name=Tenants
ansible-playbook scenarios/hub-sink/tasks/define_hub.yml -K -e clusters_count=2
ansible-playbook scenarios/hub-sink/tasks/attach_sinks.yml -K -e clusters_count=2
```

HEAL:
```
ansible-playbook toolbox/heal_node.yml -e target=1a
ansible-playbook toolbox/heal_node.yml -e target=1b
ansible-playbook toolbox/heal_node.yml -e target=1c
ansible-playbook toolbox/delete_database.yml -K -e cluster_leader=1a -e db_name=Tenants
ansible-playbook toolbox/delete_database.yml -K -e cluster_leader=2a -e db_name=Tenants
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=2a -e db_name=Tenants
ansible-playbook scenarios/hub-sink/tasks/define_hub.yml -K -e clusters_count=2
ansible-playbook scenarios/hub-sink/tasks/attach_sinks.yml -K -e clusters_count=2
ansible-playbook scenarios/hub-sink/chaos_failover.yml -K
```

---

## 5. SSH-mode (VMs / bare metal / other computers)

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
ansible-playbook -i inventory/ssh_hosts.yml toolbox/cut_link.yml -e node_a=1a -e node_b=1b

# Partition one host from its peers.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/partition_node.yml -e target=1c

# Restart RavenDB on a host (systemctl over SSH, then wait for HTTPS).
ansible-playbook -i inventory/ssh_hosts.yml toolbox/restart_ravendb.yml -e target=1a

# Hard-delete a database (REST API + stop/rm-rf/start on each peer over SSH).
ansible-playbook -i inventory/ssh_hosts.yml toolbox/delete_database.yml -e cluster_leader=1a -e db_name=Tenants

# Upgrade one host to a specific version.
ansible-playbook -i inventory/ssh_hosts.yml toolbox/upgrade_node.yml -e target=1a -e rdb_version=7.2.3
```

### Teardown

```bash
ansible-playbook -i inventory/ssh_hosts.yml playbooks/cleanup_ssh_targets.yml
```
