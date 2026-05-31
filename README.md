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

The `ravendb.ravendb` ansible collection ships with the standard ansible distribution, so you don't have to install it separately.

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

Bring your own `license.json` (from your RavenDB account) and drop it next to the four cert files. Default lookup path is `/mnt/c/dev/hub-sink/selfsignedmaterials/` - override `cert_dir` in `inventory/group_vars/all.yml` if you put it elsewhere.

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

`provision_nodes.yml` also creates a named docker volume `lab_backups` mounted at `/backups` in every container, so `toolbox/backup/backup_database.yml` outputs are visible across all containers without `docker cp`. The volume survives teardown - `docker volume rm lab_backups` to nuke.

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
| `teardown_containers.yml` | docker | removes every container on the network, drops the network, strips `/etc/hosts`. The `lab_backups` volume survives. | - | `docker_network_name` |
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

### ⚙️ tasks - ongoing-task ops

- [`set_mentor_node.yml`](toolbox/tasks/set_mentor_node.yml) - flip `MentorNode` on a pull-rep hub / sink / external task. Other task types pre-stubbed in the dispatch table.
  **Required:** `target`, `db_name`, `task_name`, `task_type` (`hub` / `sink` / `external`), `mentor_node`.

### 📦 backup - backup & restore

- [`backup_database.yml`](toolbox/backup/backup_database.yml) - trigger an on-demand Logical or Snapshot backup; waits for completion.
  **Required:** `target`, `db_name`.
  **Optional:** `backup_type` (`Backup` / `Snapshot`, default `Backup`), `backup_path`, `timeout` (default 300), `poll_interval` (default 3).
- [`restore_backup.yml`](toolbox/backup/restore_backup.yml) - restore a backup folder as a new DB; waits for completion. **`backup_path` is the FOLDER containing the `.ravendb-snapshot` file, not the file itself.**
  **Required:** `target`, `backup_path`, `new_db_name`.
  **Optional:** `timeout` (default 600), `poll_interval` (default 3).

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
- [`diagnostic_capture_cv.yml`](toolbox/diagnostic/diagnostic_capture_cv.yml) - fetch `DatabaseChangeVector` from every node of a DB; one file per node.
  **Required:** `db_name`.
  **Optional:** `nodes` (default auto-discover), `output_dir`.
- [`diagnostic_capture_doc_cv.yml`](toolbox/diagnostic/diagnostic_capture_doc_cv.yml) - for a list of doc ids, fetch `@change-vector` from every node holding the doc.
  **Required:** `db_name`, `ids` (JSON list).
  **Optional:** `nodes`, `output_dir`.
- [`diagnostic_scan_fltr.yml`](toolbox/diagnostic/diagnostic_scan_fltr.yml) - recursively grep captured CVs for literal `FLTR:`; PASS/FAIL exit.
  **Required:** `capture_dir`.
  **Optional:** `strict` (default true).
- [`diagnostic_partition_list.yml`](toolbox/diagnostic/diagnostic_partition_list.yml) - enumerate active chaos iptables rules across every node + IP→name legend.
  **Optional:** `targets`.

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
- [`wait_for_quiescence.yml`](toolbox/wait/wait_for_quiescence.yml) - poll until every node's `DatabaseChangeVector` matches (replication caught up).
  **Required:** `db_name`.
  **Optional:** `nodes` (default auto-discover), `timeout` (default 180), `poll_interval` (default 2).

---

## Tweaking

Globals in `inventory/group_vars/all.yml`:

| var | meaning |
|---|---|
| `clusters_count` | number of independent clusters |
| `nodes_per_cluster` | nodes per cluster (1..26) |
| `docker_network_name` | shared docker network name |
| `docker_image` | container base image |
| `ravendb_domain` | domain stamped into each node's PublicServerUrl |
| `rdb_version` | RavenDB version to install |
| `cert_dir` | folder containing cert + license files |

One-off overrides: `-e key=value` on the command line.

---

## More

- **[CHEATSHEET.md](CHEATSHEET.md)** - copy-paste runner for every playbook + tool, including optional-var variants.
- **[NOTES.md](NOTES.md)** - environment caveats (WSL wedge, hardware), per-tool quirks (timeseries date shell-out, delete-range inclusive bounds, backup folder path, etc.), and design decisions (why mode-aware over `_ssh` variants, why CV equality not etag stability, why no `workload_mixed.yml`).
- **`scenarios/hub-sink/`** - pre-wired hub-sink chaos scenarios that compose the toolbox tools.
- **`.github/CODEOWNERS`** - repo ownership.
