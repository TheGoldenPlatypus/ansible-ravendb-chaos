# RP-1 fixtures

Binary fixture files **required** by the RP-1 scenario.  If a referenced fixture is
missing, the scenario fails loud at the corresponding `smuggler_import` step.  The
scenario references its fixtures by name in Section 3.

## `legacy-counter.ravendbdump` (REQUIRED)

Per-family inventory item #12 from the RP-1 spec.  A legacy-format counter cannot
be created on an all-v_new cluster (v_new only writes the current counter format), so
the only way to seed one for the I-13 (a) check on RP-1's all-v_new topology is to
create it on a v_old cluster and smuggler-import the dump.

Without this file the scenario aborts in Section 3 with `Dump file not found at ...`.

### One-time creation (manual)

1. Spin up a throwaway v_old cluster (e.g. 6.2.15):
   ```bash
   ansible-playbook playbooks/provision_nodes.yml -K -e clusters_count=1 -e nodes_per_cluster=1
   ansible-playbook playbooks/install_ravendb.yml -K -e rdb_version=6.2.15
   ansible-playbook playbooks/form_clusters.yml -K -e clusters_count=1 -e nodes_per_cluster=1
   ```
2. Create db1, write doc `users/sink1/family/legacy-cnt/0`, attach a counter:
   ```bash
   ansible-playbook toolbox/db/create_database.yml -K -e cluster_leader=1a -e db_name=db1
   ansible-playbook toolbox/writes/write_docs.yml -K \
       -e target=1a -e db_name=db1 -e id_prefix=users/sink1/family/legacy-cnt -e count=1
   ansible-playbook toolbox/writes/write_counters.yml -K \
       -e target=1a -e db_name=db1 \
       -e doc_id=users/sink1/family/legacy-cnt/0 -e counter_name=likes -e delta=1
   ```
3. Export via smuggler (Studio: Settings → Export Database, OR REST):
   ```bash
   curl -sk --cert <client.pem> --cacert <ca.crt> \
       -X POST 'https://1a.hubsink.test:443/databases/db1/smuggler/export' \
       -H 'Content-Type: application/json' \
       -d '{}' \
       -o scenarios/EMR/RP1/fixtures/legacy-counter.ravendbdump
   ```
4. Teardown the throwaway lab.

After the file lands here, the next RP-1 run imports it on hub during Section 3, it
replicates to sink via the `users/sink1/*` filter, and the I-13 (a) probe in Section 8
walks it as `users/sink1/family/legacy-cnt/0`.

## What NOT to put here

Anything that can be created via the existing toolbox tools on a v_new cluster -- doc,
revision, attachment, counter (current format), time-series, tombstones, conflict, etc.
Those are all seeded inline by `rp1.yml` Section 3 without needing a fixture.
