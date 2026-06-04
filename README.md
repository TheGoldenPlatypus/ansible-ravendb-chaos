# ansible-ravendb-chaos

### A small ansible harness that spins up N docker containers (or talks to N VMs), installs RavenDB on each, and merges them into clusters of 3 (or whatever you set). Use it to chaos-test cluster behaviour locally - kill a node, partition a network, restart a service, see what happens.

<p align="center">
  <img src="assets/banner2.png" alt="ansible-ravendb-chaos" width="1600">
</p>

---

## What you need

| tool | why |
|---|---|
| docker | the containers |
| python 3 | ansible runs on python |
| ansible (>= 2.15) | the playbook runner |

Install the required ansible collections (one-time, per controller):

```bash
ansible-galaxy collection install -r requirements.yml
```

This pulls `ravendb.ravendb`, `community.docker`, `community.general`, and `ansible.posix`. Older ansible distributions used to bundle `ravendb.ravendb` automatically; newer ones don't, so installing explicitly avoids the "role `ravendb.ravendb.ravendb_node` was not found" error on a fresh clone.

### Linux / WSL2

```bash
sudo apt update
sudo apt install -y ansible python3 python3-pip docker.io openssl
```

<details>
<summary>If you don't already have docker / python3 / ansible installed (click to expand)</summary>

```bash
# docker
sudo apt install -y docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # log out + back in for the group change

# python 3
sudo apt install -y python3 python3-pip

# ansible (use pip if your distro's package is too old)
sudo apt install -y ansible
# or:  python3 -m pip install --user "ansible-core>=2.15"
```

</details>

### Windows

Install **WSL2 + Docker Desktop** (toggle "Use WSL 2 based engine"), install Ubuntu from the Microsoft Store, follow the Linux steps inside Ubuntu. Keep the project under `~/...` inside WSL, not `/mnt/c/...` (files there are world-writable from ansible's POV and `ansible.cfg` gets ignored).

---

## Certs & license

Four pre-built cert files (`ca.crt`, `ca.key`, `server.pfx`, `client.pfx`) live in a shared drive folder:

> https://drive.google.com/file/d/1frqQp_3ZeSvoDfTBhj8YoSO6XgFc76q8/view?usp=sharing

Bring your own `license.json` (from your RavenDB account) and drop it next to the four cert files. Default lookup path is `/home/kaiju-1/EMR/selfsignedmaterials/` - override `cert_dir` in `inventory/group_vars/all.yml` if you put it elsewhere.

---

## Layout convention

Every node's name is `<cluster_id><node_letter>`. `cluster_id` is an integer, `node_letter` is `a..z` (max 26 nodes per cluster). The node ending in `a` is the cluster's leader. Studio tags shown inside each cluster are `A`, `B`, `C`...

| cluster_id | nodes |
|---|---|
| 1 | 1a, 1b, 1c |
| 2 | 2a, 2b, 2c |
| ... | ... |

Two scaling knobs in `inventory/group_vars/all.yml`: `clusters_count`, `nodes_per_cluster`. Override on the CLI:

```bash
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=3 -e nodes_per_cluster=4
```

---

## Bring-up - docker mode

```bash
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=3 -e nodes_per_cluster=3
ansible-playbook playbooks/install_ravendb.yml
ansible-playbook playbooks/form_clusters.yml -K
```

Studio: open `https://1a.hubsink.test/studio`. Teardown: `ansible-playbook playbooks/teardown_containers.yml -K`.

`provision_nodes.yml` also creates a named docker volume `lab_backups` mounted at `/backups` in every container, so `toolbox/backup/backup_database.yml` outputs are visible across all containers without `docker cp`. The volume is removed by `teardown_containers.yml`.

### Parallel labs on the same docker host

Three concurrent labs without name collisions: override `cluster_id_start` (default 1) and `docker_network_name` per run. Each parallel run claims a disjoint cluster-id range:

```bash
# Run A: clusters 1, network alphanet
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=1 -e nodes_per_cluster=3 \
    -e cluster_id_start=1 -e docker_network_name=alphanet

# Run B: clusters 4-5, network betanet
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=2 -e nodes_per_cluster=3 \
    -e cluster_id_start=4 -e docker_network_name=betanet
```

Containers in Run A are named `1a/1b/1c`; in Run B `4a/4b/4c/5a/5b/5c`. `form_clusters` and `teardown_containers` both scope to the configured `docker_network_name`, so the two labs are fully isolated. EMR scenarios accept matching `-e cluster_id_start=N -e docker_network_name=NAME` overrides — see [scenarios/EMR/README.md](scenarios/EMR/README.md) for the parallel-run pattern across RV-1 / RP-1 / RPV-1.

## Bring-up - SSH mode

```bash
cp inventory/ssh_hosts.yml.example inventory/ssh_hosts.yml    # then fill in your hosts
ansible-playbook -i inventory/ssh_hosts.yml playbooks/setup_ssh_targets.yml
ansible-playbook -i inventory/ssh_hosts.yml playbooks/install_ravendb.yml
ansible-playbook -i inventory/ssh_hosts.yml playbooks/form_clusters.yml
```

Teardown: `ansible-playbook -i inventory/ssh_hosts.yml playbooks/cleanup_ssh_targets.yml` (uninstalls RavenDB, flushes chaos rules, strips `/etc/hosts`; the machines themselves stay).

Network-chaos tools have `_ssh` variants (`toolbox/network/cut_link_ssh.yml`, etc.) because the docker tools use `docker_container_exec` and don't speak SSH. **Mode-aware** tools (e.g. `partition_set.yml`, `heal_all.yml`, every `diagnostic_*` and `wait_*` tool) auto-detect the mode and don't need a separate file.

**See [NOTES.md](NOTES.md) for the WSL/Hyper-V wedge, hardware caveats, SSH-mode backups, and per-tool quirks.**

---

## Playbooks (full reference)

All playbooks live under `playbooks/`. Pass `-K` whenever the playbook needs sudo (most do, for `/etc/hosts` edits). `-e key=value` for any input.

| playbook | mode | what it does | required vars | optional vars |
|---|---|---|---|---|
| `provision_nodes.yml` | docker | creates the shared docker network + `lab_backups` volume + one privileged systemd-ready container per `<cluster_id><letter>` | - | `clusters_count`<br>`nodes_per_cluster`<br>`container_memory`<br>`docker_network_name`<br>`docker_image`<br>`backups_volume_name` |
| `setup_ssh_targets.yml` | ssh | verifies SSH reachability + installs `iptables` / `python3` apt prereqs + creates `/backups` | (inventory must populate `ravendb_nodes`) | - |
| `install_ravendb.yml` | both | discovers targets, trusts CA on controller, runs `ravendb.ravendb.ravendb_node` role, registers admin cert on cluster leaders, chowns `/backups` to the ravendb user | - | `rdb_version`<br>`custom_build` (+ `--skip-tags download`)<br>`cert_dir`<br>`ravendb_domain` |
| `form_clusters.yml` | both | writes `/etc/hosts` on controller + each host, then merges each cluster's nodes into one RavenDB cluster via the `ravendb.ravendb.node` module | - | `clusters_count`<br>`nodes_per_cluster`<br>`cert_dir`<br>`ravendb_domain` |
| `add_node.yml` | docker | adds one extra container; can join it to an existing cluster, stay passive, or bootstrap as its own 1-node cluster | `node_name` | `join_to`<br>`node_tag`<br>`passive`<br>`custom_build` (+ `--skip-tags download`)<br>`container_memory`<br>`rdb_version` |
| `teardown_containers.yml` | docker | removes every container on the network, drops the network, removes the `lab_backups` volume, wipes `captures/` at repo root, kills any leftover background workloads (`workload_w1.sh` + `/tmp/w1-*.pid`), strips `/etc/hosts`. | - | `docker_network_name`<br>`backups_volume_name` |
| `cleanup_ssh_targets.yml` | ssh | uninstalls RavenDB via the role's `state=absent` on each host, flushes leftover chaos rules, strips `/etc/hosts`. Hosts themselves stay. | - | - |

---

## Toolbox (full reference)

Single-purpose playbooks under `toolbox/<group>/`. Each one is CLI-runnable on its own and importable from a scenario playbook via `import_playbook`.

**Naming convention:**

- **Unprefixed** = mutator. Changes state.
- **`wait_for_*`** = sync. Blocks until a condition is met, with a timeout.
- **`diagnostic_*`** = read-only. Never changes state.

Output for `diagnostic_capture_*` tools lands under repo-root `captures/` (gitignored).
Each tool's file header has the full inputs + run examples - click the link to read it.

### 🌐 network - connectivity chaos

- [`cut_link.yml`](toolbox/network/cut_link.yml) - (docker) REJECT all TCP between two containers, forces TCP reset.       **Required:** `node_a`, `node_b`.
- [`cut_link_ssh.yml`](toolbox/network/cut_link_ssh.yml) - (ssh) DROP all TCP between two inventory hosts; silent, no RST. **Required:** `node_a`, `node_b`.
- [`restore_link.yml`](toolbox/network/restore_link.yml) - (docker) symmetric inverse of `cut_link`.
  **Required:** `node_a`, `node_b`.
- [`restore_link_ssh.yml`](toolbox/network/restore_link_ssh.yml) - (ssh) symmetric inverse of `cut_link_ssh`.
  **Required:** `node_a`, `node_b`.
- [`partition_node.yml`](toolbox/network/partition_node.yml) - (docker) cut every link between one node and every cluster peer (peers via `/cluster/topology`).
  **Required:** `target`.
- [`partition_node_ssh.yml`](toolbox/network/partition_node_ssh.yml) - (ssh) same, over SSH.
  **Required:** `target`.
- [`heal_node.yml`](toolbox/network/heal_node.yml) - (docker) symmetric inverse of `partition_node`.
  **Required:** `target`.
- [`heal_node_ssh.yml`](toolbox/network/heal_node_ssh.yml) - (ssh) symmetric inverse of `partition_node_ssh`.
  **Required:** `target`.
- [`partition_set.yml`](toolbox/network/partition_set.yml) - (both) bidirectionally cut every pair in `set_a × set_b`, with TCP-connect validation; cross-cluster generalization of `cut_link`.
  **Required:** `set_a` (JSON list), `set_b` (JSON list).
- [`heal_all.yml`](toolbox/network/heal_all.yml) - (both) flush every chaos iptables rule on every node in one shot.
  **Optional:** `targets` (JSON list; default = auto-discover all).

### 💾 db - database lifecycle

- [`create_database.yml`](toolbox/db/create_database.yml) - create a DB via the ravendb collection's `database` module.
  **Required:** `cluster_leader`, `db_name`.
  **Optional:** `replication_factor` (default 3).
- [`delete_database.yml`](toolbox/db/delete_database.yml) - hard-delete + poll until gone (stops service, wipes on-disk dir, restarts on every peer).
  **Required:** `cluster_leader`, `db_name`.
  **Optional:** `timeout_secs` (default 60).
- [`configure_revisions.yml`](toolbox/db/configure_revisions.yml) - enable document revisions with `MinimumRevisionsToKeep` on Default config.
  **Required:** `target`, `db_name`.
  **Optional:** `minimum_revisions` (default 100).

### ✏️ writes - mutating writes

- [`write_docs.yml`](toolbox/writes/write_docs.yml) - PUT N docs to a target node (single id-prefix).
  **Required:** `target`, `db_name`, `count`.
  **Optional:** `id_prefix` (default `micro/doc`).
- [`write_docs_interleaved.yml`](toolbox/writes/write_docs_interleaved.yml) - PUT N docs round-robin across multiple id-prefixes.
  **Required:** `target`, `db_name`, `count`, `prefixes` (JSON list).
- [`write_docs_freeform.yml`](toolbox/writes/write_docs_freeform.yml) - PUT N freeform docs (random GUID id, null collection).
  **Required:** `target`, `db_name`, `count`.
- [`delete_docs.yml`](toolbox/writes/delete_docs.yml) - DELETE by explicit id-list OR by id-prefix + count.
  **Required:** `target`, `db_name`, and either `ids` or `id_prefix`+`count`.
- [`write_attachments.yml`](toolbox/writes/write_attachments.yml) - PUT N attachments onto existing docs.
  **Required:** `target`, `db_name`, `count`.
  **Optional:** `doc_id_prefix` (default `micro/doc`), `attachment_name` (default `data`), `payload`.
- [`write_counters.yml`](toolbox/writes/write_counters.yml) - increment a named counter on a doc N times.
  **Required:** `target`, `db_name`, `doc_id`.
  **Optional:** `counter_name` (default `Likes`), `delta` (default 1), `repeat` (default 1).
- [`write_timeseries.yml`](toolbox/writes/write_timeseries.yml) - append N TS entries OR delete a range (inclusive on both bounds).
  **Required:** `target`, `db_name`, `doc_id`.
  **Optional:** `ts_name` (default `Heartrate`), `count` (default 100), `start_timestamp`, `interval_seconds` (default 6), `delete_from`+`delete_to` (switches to delete-range mode).
- [`restore_revision.yml`](toolbox/writes/restore_revision.yml) - restore an older revision as the new live doc; exercises the attachment-from-revision recreate path.
  **Required:** `target`, `db_name`, `doc_id`, `revision_cv`.
- [`write_docs_revisions.yml`](toolbox/writes/write_docs_revisions.yml) - PUT N docs × M revisions each, with DISTINCT bodies per PUT so RavenDB actually records each as a new revision (`write_docs.yml` dedups identical bodies into 1 revision).
  **Required:** `target`, `db_name`, `count`, `revs_per_doc`.
  **Optional:** `id_prefix` (default `seed`).

### ⚙️ tasks - ongoing-task ops

- [`set_mentor_node.yml`](toolbox/tasks/set_mentor_node.yml) - flip `MentorNode` on a pull-rep hub / sink / external task. Other task types pre-stubbed in the dispatch table.
  **Required:** `target`, `db_name`, `task_name`, `task_type` (`hub` / `sink` / `external`), `mentor_node`.

### 📦 backup - backup, restore, smuggler import

- [`backup_database.yml`](toolbox/backup/backup_database.yml) - trigger an on-demand Logical or Snapshot backup; waits for completion.
  **Required:** `target`, `db_name`.
  **Optional:** `backup_type` (`Backup` / `Snapshot`, default `Backup`), `backup_path`, `timeout` (default 300), `poll_interval` (default 3).
- [`restore_backup.yml`](toolbox/backup/restore_backup.yml) - restore a backup folder as a new DB; waits for completion. **`backup_path` is the FOLDER containing the `.ravendb-snapshot` file, not the file itself.**
  **Required:** `target`, `backup_path`, `new_db_name`.
  **Optional:** `timeout` (default 600), `poll_interval` (default 3).
- [`smuggler_import.yml`](toolbox/backup/smuggler_import.yml) - POST a `.ravendbdump` file from the controller into an existing DB via `/databases/<db>/smuggler/import`. Used by RP-1 to seed a legacy-format counter from a pre-built v_old dump on an all-v_new cluster. Different from `restore_backup.yml` (which restores TO A NEW database from a container-local path).
  **Required:** `target`, `db_name`, `dump_path` (absolute, on the controller).
  **Optional:** `skip_if_missing` (default false — when true, a missing dump_path becomes a no-op).

### 🔁 replication - pull-replication setup

- [`define_hub.yml`](toolbox/replication/define_hub.yml) - hub-side: define the Hub task + mint per-sink certs + register Hub Access entries with per-sink filters.
  **Required:** `hub_leader`, `db_name`, `hub_task_name`, `sink_cluster_ids` (JSON list), `sink_allowed_paths` (dict: `<sink_id>` → list of allowed `HubToSink` prefixes).
  **Optional:** `replication_mode` (default `"HubToSink, SinkToHub"`), `sink_to_hub_paths` (dict per sink id → list; default `["*"]`), `replication_certs_dir` (default `<repo_root>/replication-certs/`).
- [`attach_sinks.yml`](toolbox/replication/attach_sinks.yml) - sink-side: for each sink cluster, create the connection string + sink-pull task using the per-sink PFX `define_hub.yml` wrote.
  **Required:** `hub_topology_urls` (JSON list of every hub node's URL), `db_name`, `hub_task_name`, `sink_cluster_ids`, `sink_allowed_paths` (must match `define_hub.yml`'s spec).
  **Optional:** `replication_mode`, `sink_to_hub_paths`, `connection_string_name` (default `hub-connection`), `replication_certs_dir`, `sink_leader_template` (default `{id}a.{{ ravendb_domain }}`).

### 📨 subscriptions

- [`open_subscription.yml`](toolbox/subscriptions/open_subscription.yml) - **STUB.** Running it fails with implementation guidance. M10 needs a Python consumer using the RavenDB client; see file header for the spec.

### 🛠 service - node operations

- [`restart_ravendb.yml`](toolbox/service/restart_ravendb.yml) - `systemctl restart ravendb` + wait for HTTPS to come back.
  **Required:** `target`.
  **Optional:** `timeout_secs` (default 120).
- [`upgrade_node.yml`](toolbox/service/upgrade_node.yml) - upgrade (or downgrade) RavenDB on one node.
  **Required:** `target`.
  **Optional:** `rdb_version`, `custom_build` (+ `--skip-tags download`), `timeout_secs`.
- [`force_cluster_asymmetry.yml`](toolbox/service/force_cluster_asymmetry.yml) - upgrade specific nodes to specific versions per a map (shells out to `upgrade_node.yml`).
  **Required:** `version_map` (JSON dict).
- [`remove_node.yml`](toolbox/service/remove_node.yml) - remove a node from its cluster via the admin API; verifies.
  **Required:** `cluster_leader`, `target_tag`.

### 🔍 diagnostic - read-only inspection

- [`diagnostic_doc_count.yml`](toolbox/diagnostic/diagnostic_doc_count.yml) - print `CountOfDocuments` from `/stats`.
  **Required:** `target`, `db_name`.
- [`diagnostic_replication.yml`](toolbox/diagnostic/diagnostic_replication.yml) - dump incoming + outgoing replication connections for a DB.
  **Required:** `target`, `db_name`.
- [`diagnostic_capture_cv.yml`](toolbox/diagnostic/diagnostic_capture_cv.yml) - fetch `DatabaseChangeVector` from every probed node; one file per node.
  **Required:** `db_name`, `nodes` (JSON list).
  **Optional:** `output_dir` (default `<repo_root>/captures/cv-<db>-<ts>/`).
- [`diagnostic_capture_doc_cv.yml`](toolbox/diagnostic/diagnostic_capture_doc_cv.yml) - for a list of doc ids, fetch `@change-vector` from every probed node.
  **Required:** `db_name`, `ids` (JSON list), `nodes` (JSON list).
  **Optional:** `output_dir`.
- [`diagnostic_scan_fltr.yml`](toolbox/diagnostic/diagnostic_scan_fltr.yml) - recursively grep captured CVs for literal `FLTR:`; PASS/FAIL exit.
  **Required:** `capture_dir`.
  **Optional:** `strict` (default true).
- [`diagnostic_partition_list.yml`](toolbox/diagnostic/diagnostic_partition_list.yml) - enumerate active chaos iptables rules across every node + IP→name legend.
  **Optional:** `targets`.
- [`diagnostic_doc_count_parity.yml`](toolbox/diagnostic/diagnostic_doc_count_parity.yml) - assert every probed node reports the same `CountOfDocuments`.
  **Required:** `db_name`, `nodes`.
- [`diagnostic_doc_id_set_parity.yml`](toolbox/diagnostic/diagnostic_doc_id_set_parity.yml) - for a sampled probe set, assert each id is either present-on-all or absent-on-all (no splits).
  **Required:** `db_name`, `nodes`, and either `ids` (JSON list) OR (`id_prefix` + `count`).
- [`diagnostic_revision_count_parity.yml`](toolbox/diagnostic/diagnostic_revision_count_parity.yml) - per-doc revision count parity across nodes; catches duplicates / missing revs.
  **Required:** `db_name`, `nodes`, and either `ids` OR (`id_prefix` + `count`).
  **Optional:** `expected_count` (also asserts every node reports exactly this count), `page_size` (default 1024).
- [`diagnostic_schema_version.yml`](toolbox/diagnostic/diagnostic_schema_version.yml) - read `/build/version` per node; dump, optionally assert parity or expected version.
  **Required:** `nodes`.
  **Optional:** `require_parity` (default false), `expected_version` (substring match).
- [`diagnostic_size_envelope.yml`](toolbox/diagnostic/diagnostic_size_envelope.yml) - whole-DB `SizeOnDisk` envelope check. First call captures baseline; subsequent calls (same `baseline_file`) compare and assert each node's growth ≤ `max_growth_pct`.
  **Required:** `db_name`, `nodes`, `baseline_file` (absolute path).
  **Optional:** `max_growth_pct` (default 300 — sized to catch leaks, not normal writer-side bookkeeping).
- [`diagnostic_orphan_revisions.yml`](toolbox/diagnostic/diagnostic_orphan_revisions.yml) - Triggers `POST /admin/revisions/orphaned/adopt` on each node, polls until the operation completes, asserts `AdoptedCount == 0` everywhere. Detects revisions whose parent document is missing.
  **Required:** `db_name`, `nodes`.
  **Optional:** `budget_secs` (default 60).
- [`diagnostic_equal_stats.yml`](toolbox/diagnostic/diagnostic_equal_stats.yml) - Asserts non-document `/stats` aggregates are equal across nodes — attachments, counters, time-series. Pairs with `diagnostic_doc_count_parity` for full entity-count parity coverage when a scenario writes attachments / counters / TS.
  **Required:** `db_name`, `nodes`.
  **Optional:** `aspects` (CSV subset of `attachments,counters,timeseries`; default all three).
- [`diagnostic_filter_compliance.yml`](toolbox/diagnostic/diagnostic_filter_compliance.yml) - Enumerates every doc on a sink cluster and asserts each id matches at least one configured allowed-prefix. Catches "doc that should have been filtered out reached the sink anyway."
  **Required:** `sink_cluster_leader`, `db_name`.
  **Optional:** `allowed_prefixes` (JSON list; defaults to fetching from `DatabaseRecord.SinkPullReplications[].AllowedHubToSinkPaths`).
- [`diagnostic_stored_item_cv_split.yml`](toolbox/diagnostic/diagnostic_stored_item_cv_split.yml) - Inspects the stored CV shape on a target node. Two modes: `expect=split` asserts a new-lane "split-form" CV (post-upgrade / on a v_new receiver after filtered ingress); `expect=raw` asserts the old raw-CV form (pre-upgrade baseline). Delimiter heuristic — see the file header.
  **Required:** `db_name`, `target`, `doc_ids` (JSON list).
  **Optional:** `delimiter` (default `|`), `expect` (default `split`).
- [`diagnostic_db_cv_order_side_only.yml`](toolbox/diagnostic/diagnostic_db_cv_order_side_only.yml) - On every node in a receiver-group cluster, asserts every entry in `/stats.DatabaseChangeVector` has a node tag from the receiver-group tags set. Source-side tag leakage = fail. Useful as a regression guard for the CV-boundary fix on filtered-pull receivers.
  **Required:** `db_name`, `receiver_group_nodes`.
  **Optional:** `receiver_group_tags` (derived from node names if omitted).
- [`diagnostic_cv_boundary_by_dbid.yml`](toolbox/diagnostic/diagnostic_cv_boundary_by_dbid.yml) - Directional CV-boundary check (I-13 b) by DatabaseId rather than by tag-letter. Asserts the order-side of every receiver-cluster CV contains no source-cluster dbid. N/A on legacy raw-CV form (no `|` delimiter); `strict_v_new=true` forces failure if any receiver is still legacy.
  **Required:** `db_name`, `source_nodes`, `receiver_nodes`.
  **Optional:** `strict_v_new` (default false).
- [`diagnostic_cross_sink_isolation.yml`](toolbox/diagnostic/diagnostic_cross_sink_isolation.yml) - Probes deterministic sibling-sink doc ids on a sink leader; fails on any 200 response (sibling-sink data leaked across).
  **Required:** `db_name`, `sink_cluster_leader`, `forbidden_prefixes` (JSON list).
  **Optional:** `sample_per_prefix` (default 100).
- [`diagnostic_lane_inert.yml`](toolbox/diagnostic/diagnostic_lane_inert.yml) - Asserts the v_new lane stayed dormant: samples revision CVs across nodes/prefixes and fails if any contains `|` (the new-lane "order\|version" delimiter pinned by the PR).
  **Required:** `db_name`, `nodes`, `id_prefixes` (JSON list).
  **Optional:** `sample_per_prefix` (default 25).
- [`diagnostic_stats_parity.yml`](toolbox/diagnostic/diagnostic_stats_parity.yml) - Consolidated `/stats` parity across nodes (12 fields in 1 GET/node). Replaces the doc_count_parity + equal_stats chain. Prints a single readable table; asserts parity on 9 cross-node-stable fields; `CountOfCounterEntries`, `CountOfTimeSeriesSegments`, and `SizeOnDisk` are shown but NOT asserted (per-node-intrinsic per upstream guidance). Drifts on the info-only fields surface in a prominent `INFORMATIONAL DRIFT` flag block.
  **Required:** `db_name`, `nodes`.
  **Optional:** `assert_fields` (subset list), `informational_only` (default false — when true, prints the table but skips the assertion).

### ⏱ wait - synchronization gates

- [`wait_for_healthy.yml`](toolbox/wait/wait_for_healthy.yml) - wrap `ravendb.ravendb.healthcheck`.
  **Required:** `cluster_leader`, `checks` (CSV: `node_alive`, `cluster_connectivity`).
  **Optional:** `max_wait` (default 120).
- [`wait_for_rehab.yml`](toolbox/wait/wait_for_rehab.yml) - block until target node enters DB-level rehab (Promotables / Rehabs).
  **Required:** `cluster_leader`, `db_name`, `target`.
  **Optional:** `timeout_secs` (default 120).
- [`wait_for_member.yml`](toolbox/wait/wait_for_member.yml) - block until target node is back as a full Member.
  **Required:** `cluster_leader`, `db_name`, `target`.
  **Optional:** `timeout_secs` (default 300).
- [`wait_for_quiescence.yml`](toolbox/wait/wait_for_quiescence.yml) - poll until every probed node's `DatabaseChangeVector` matches (cross-node replication caught up).
  **Required:** `db_name`, `nodes` (JSON list).
  **Optional:** `timeout` (default 180), `poll_interval` (default 2).
- [`wait_for_docs_drain.yml`](toolbox/wait/wait_for_docs_drain.yml) - poll until every probed node's `DatabaseChangeVector` stops changing across two consecutive polls (per-node "writes have flushed" check; distinct from quiescence).
  **Required:** `db_name`, `nodes`.
  **Optional:** `timeout` (default 180), `poll_interval` (default 3).
- [`wait_for_conflicts_resolved.yml`](toolbox/wait/wait_for_conflicts_resolved.yml) - poll `/replication/conflicts` until every probed node reports zero.
  **Required:** `db_name`, `nodes`.
  **Optional:** `timeout` (default 60), `poll_interval` (default 2).
- [`wait_for_leader.yml`](toolbox/wait/wait_for_leader.yml) - poll `/cluster/topology` until a Leader exists; optionally pin to a specific tag.
  **Required:** `target` (any reachable node).
  **Optional:** `timeout_secs` (default 60), `expected_leader` (single-letter tag).
- [`wait_for_marker_propagation.yml`](toolbox/wait/wait_for_marker_propagation.yml) - PUT a uniquely-IDed marker doc on `source`, then poll every target until it appears (end-to-end "replication is flowing" check).
  **Required:** `db_name`, `source`, `targets` (JSON list).
  **Optional:** `timeout_secs` (default 60).
- [`wait_for_etag_parity.yml`](toolbox/wait/wait_for_etag_parity.yml) - Per-node `LastDatabaseEtag` stability — two snapshots ~3-5s apart, every node's etag identical between the two reads = drained. spec-aligned "wait by etag"; less log-noisy than `wait_for_quiescence` (single shell+curl loop, integer compare).
  **Required:** `db_name`, `nodes`.
  **Optional:** `timeout` (default 180), `poll_interval` (default 3).
- [`wait_for_stats_field_parity.yml`](toolbox/wait/wait_for_stats_field_parity.yml) - Polls `/stats` until specified count-field(s) match across nodes. Use after a workload stops to wait for background cleanup (tombstone purge etc.) to bring per-node counts into parity before asserting.
  **Required:** `db_name`, `nodes`.
  **Optional:** `fields` (default `["CountOfTombstones"]`), `timeout` (default 180), `poll_interval` (default 3).

### 🚦 control - scenario flow control

- [`pause_gate.yml`](toolbox/control/pause_gate.yml) - Section banner + opt-in pause.  Always prints a 3-line ASCII banner (`>>>>>  SECTION X -- …`); pauses for ENTER when `pause_between_sections=true`.  Used at the top of every section in the EMR scenarios.
  **Required:** `section_label`.
  **Optional:** `pause_between_sections` (default false).

### 🏃 workloads - background-workload process management

- [`wait_for_workload_started.yml`](toolbox/workloads/wait_for_workload_started.yml) - block until a workload's pidfile appears on the controller (confirms a fire-and-forget background launch actually started).
  **Required:** `pidfile`.
  **Optional:** `timeout` (default 30).
- [`stop_workload.yml`](toolbox/workloads/stop_workload.yml) - TERM (grace window) then KILL via pidfile; idempotent if the workload already exited.
  **Required:** `pidfile`.
  **Optional:** `grace_secs` (default 2).
- [`assert_workload_alive.yml`](toolbox/workloads/assert_workload_alive.yml) - Asserts a background workload is still running (pidfile present + `kill -0 <pid>` succeeds). Use BEFORE `stop_workload` to catch silently-died workers (OOM, crash, etc.) — without it `stop_workload` shrugs "WORKLOAD ALREADY EXITED" and the scenario passes with degraded load.
  **Required:** `pidfile`.

---

## Tweaking

Globals in `inventory/group_vars/all.yml`:

| var | meaning |
|---|---|
| `clusters_count` | number of independent clusters |
| `nodes_per_cluster` | nodes per cluster (1..26) |
| `cluster_id_start` | starting cluster_id (default 1 → containers `1a/1b/.../2a/...`).  Override per-run for parallel labs on the same docker host. |
| `docker_network_name` | shared docker network name (override per-run for isolated parallel labs) |
| `docker_image` | container base image |
| `ravendb_domain` | domain stamped into each node's PublicServerUrl |
| `rdb_version` | RavenDB version to install |
| `cert_dir` | folder containing cert + license files |

One-off overrides: `-e key=value` on the command line.

**`quiet` (default `true`)**: looping tasks across the toolbox (`write_docs`, `delete_docs`, `diagnostic_doc_id_set_parity`, `diagnostic_revision_count_parity`, the `_wait_for_*_attempt` files, etc.) carry `no_log: "{{ quiet | default(true) | bool }}"` on the per-item loop. Run output stays readable — you see PLAY banners, validate, and the final `Done`/PASS/FAIL summary, but not per-item PUT/GET spam. Pass `-e quiet=false` to a single tool when you need to see every item for debugging. Failures still print full detail because `result_format=yaml` dumps the failed task regardless of `no_log` on siblings.

---

## More

- **[CHEATSHEET.md](CHEATSHEET.md)** - copy-paste runner for every playbook + tool, including optional-var variants.
- **[NOTES.md](NOTES.md)** - environment caveats (WSL wedge, hardware), per-tool quirks (timeseries date shell-out, delete-range inclusive bounds, backup folder path, etc.), and design decisions (why mode-aware over `_ssh` variants, why CV equality not etag stability, why no `workload_mixed.yml`).
- **[scenarios/EMR/README.md](scenarios/EMR/README.md)** - end-to-end EMR test scenarios.  RV-1 (single-cluster v_62→v_new + churn + 1M-rev), RP-1 (CV-boundary regression guard), RPV-1 (cross-cluster rolling upgrade, 3 variants).  Each scenario composes toolbox tools, ships with a `run.sh` wrapper, and is parallel-safe via `cluster_id_start` / `docker_network_name` overrides.  Full spec in [EMR_TESTING_PLAN/](EMR_TESTING_PLAN/).
- **`scripts/build_ravendb_pr.sh`** - build a RavenDB `.deb` (Studio included) from any ravendb/ravendb PR. Output lands in `builds/raven-pr<N>.deb`; hand it to `install_ravendb.yml -e custom_build=...`.
- **`.github/CODEOWNERS`** - repo ownership.
