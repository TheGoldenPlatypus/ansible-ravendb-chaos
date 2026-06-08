# RV-3 fixtures

RV-3's spec calls for **"mixed-form" revisions** in the phase (a) and phase (b) seeds —
a mix of v_62 raw-CV revisions and v_new hashed-form revisions on the same docId pool.
On an all-v_new cluster, fresh writes only produce hashed-form, so the legacy half has
to come from a pre-built smuggler dump exported from a v_62 cluster.

## `legacy-revs.ravendbdump`

A smuggler dump containing N docs × M revs seeded on a `v_62` (6.2.x) cluster, then
exported.  The on-disk raw-CV format is preserved by smuggler.  When `rv3.yml` imports
this fixture before the hashed-form seed, the resulting database has revisions in BOTH
forms

**Status: REQUIRED.**  `rv3.yml` step 1 asserts this file exists and fails the run if
it doesn't - spec mandates mixed-form seed; a hashed-only seed would silently
neuter the cross-form invariant.  Rebuild via the recipe below if the fixture is lost.

## Building the fixture (one-time)

On any host that can spin a v_62 cluster:

```bash
# 1. Bring up a single-node v_62 lab
ansible-playbook playbooks/teardown_containers.yml -K -e docker_network_name=fixturenet
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=1 -e nodes_per_cluster=1 -e docker_network_name=fixturenet
ansible-playbook playbooks/install_ravendb.yml -e rdb_version=6.2.15 -e docker_network_name=fixturenet
ansible-playbook playbooks/form_clusters.yml -e clusters_count=1 -e nodes_per_cluster=1 -e docker_network_name=fixturenet -K

# 2. Create db + seed N docs x M revs on v_62 (writes land as raw-CV)
ansible-playbook toolbox/db/create_database.yml -e cluster_leader=1a -e db_name=legacy -e replication_factor=1
ansible-playbook toolbox/db/configure_revisions.yml -e target=1a -e db_name=legacy
ansible-playbook toolbox/writes/write_docs_revisions.yml \
    -e target=1a -e db_name=legacy \
    -e id_prefix=users -e collection=Users \
    -e count=10000 -e revs_per_doc=3

# 3. Smuggler-export to the fixture path
mkdir -p scenarios/EMR/RV3/fixtures
ansible-playbook toolbox/smuggler/export_dump.yml \
    -e target=1a -e db_name=legacy \
    -e dump_path=$PWD/scenarios/EMR/RV3/fixtures/legacy-revs.ravendbdump

# 4. Commit the dump alongside this README so all checkouts have it
git add scenarios/EMR/RV3/fixtures/legacy-revs.ravendbdump
```

Once committed, every RV-3 run picks it up automatically — no per-run setup.
