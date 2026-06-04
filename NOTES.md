# Notes, caveats, design decisions

The stuff that doesn't belong in the main README but a future maintainer will want.

---

## Environment caveats

### WSL2 + Hyper-V wedge

Driving SSH-mode chaos from WSL2 is unreliable.

WSL2 with `networkingMode=mirrored` shares the Windows host's network through a Hyper-V virtual
switch. Windows's stateful filter sits in the path. When iptables-based chaos fires (either
docker-mode REJECT or SSH-mode DROP), the resulting TCP failure patterns cause Windows to silently
wedge subsequent WSL→VM TCP. **Symptom:** SSH from WSL to the cut VMs hangs forever (ICMP ping
still works), even though iptables on the VMs has nothing blocking the controller's source IP.

**Fix:** drive chaos scenarios from a non-WSL Linux controller. The toolbox itself works fine on
WSL for docker-mode; the wedge only bites SSH-mode chaos.

### Hardware capacity

Chaos scenarios assume RavenDB restarts in seconds. On nodes with < 1 GB RAM (e.g. Raspberry Pi
Zero), `systemctl restart ravendb` can take ~50s+ and timing-sensitive scenarios won't behave
usefully. **Aim for ≥ 2 GB RAM per node for real testing.**

### SSH per-host `/backups` (no shared FS)

`setup_ssh_targets.yml` creates `/backups` on each VM with `0777` perms (parity with docker's
`/backups` path), but **every VM has its own local `/backups`** -- there's no shared filesystem
on VMs.

- Same-host backup→restore: works out of the box.
- Cross-host restore: you must copy the backup folder between hosts
  yourself:
  ```bash
  scp -r worker4@192.168.100.14:/backups/<folder> /tmp/relay
  scp -r /tmp/relay worker5@192.168.100.15:/backups/<folder>
  ```

The backup/restore tools are mode-agnostic at the REST level -- only the docker setup happens to
provide a shared `lab_backups` volume.

---

## Tool-specific quirks

### `wait_for_quiescence.yml`, `wait_for_docs_drain.yml`, `wait_for_conflicts_resolved.yml` (`toolbox/wait/`)

- Single shell task per call (bash + curl + jq).  The polling loop runs inside the script, so
  the ansible log gets one task line per wait, not one per attempt.  Requires `jq` on the
  controller (`sudo apt install jq` -- standard on Ubuntu/WSL).
- Drops nodes returning 404/500/503 from the convergence set on the first poll (those don't
  have the DB).  Mid-run transient curl failures don't abort -- the iteration is just skipped.
- On TIMEOUT the script prints `last_cvs` (quiescence/drain) or `last_counts` (conflicts) so
  the next debugger can see exactly what didn't converge.
- The previous `_wait_for_*_attempt.yml` include-helpers were removed.  If you see a scenario
  still referencing them, update the scenario.

### `write_timeseries.yml` (`toolbox/writes/`)

- Append mode shells out to `date -u -d` once per entry to compute timestamps. **Slow for large
  `count`** (~10ms per entry). Acceptable for chaos seeding (count ≤ 100); not for stress runs.
- Delete-range mode is **inclusive on both bounds** -- `[from, to]`, not `[from, to)`. RavenDB
  semantics, not ours.

### `write_attachments.yml`, `write_counters.yml` (`toolbox/writes/`)

- Both require the parent doc to exist. Run `write_docs.yml` first or PUT the doc via curl.

### `write_docs.yml` (`toolbox/writes/`)

- PUTs the same fixed body every call. **RavenDB dedups identical-content PUTs into a single
  revision.** If a test needs distinct revisions (e.g. for `restore_revision.yml`), PUT distinct
  bodies via curl manually:
  ```bash
  for v in 1 2 3; do
    curl -sk --cert-type P12 --cert <client.pfx>: \
      -X PUT "https://1a.hubsink.test:443/databases/<db>/docs?id=<id>" \
      -H "Content-Type: application/json" \
      -d "{\"version\":$v,\"@metadata\":{\"@collection\":\"MicroDocs\"}}"
  done
  ```

### `restore_backup.yml` (`toolbox/backup/`)

- `backup_path` is the **folder** containing the `.ravendb-snapshot` (or `.ravendb-backup`)
  file, not the file itself. RavenDB nests by `<db>-<tag>-snapshot/<timestamp>.ravendb-snapshot`,
  so:
  ```bash
  docker exec 1a find /backups/snap-test -name '*.ravendb-snapshot' -printf '%h\n' | head -1
  # /backups/snap-test/2026-05-28-18-13-36.ravendb-Tenants-A-snapshot
  # That folder is the backup_path you pass.
  ```

### `set_mentor_node.yml` (`toolbox/tasks/`)

- Wired for `task_type=hub`, `sink`, `external`. Seven other task types (`backup`, `subscription`,
  `raven_etl`, `sql_etl`, `olap_etl`, `elastic_etl`, `queue_etl`) are pre-stubbed in the
  `_dispatch` table -- uncomment + verify body shape on first use.

### `open_subscription.yml` (`toolbox/subscriptions/`)

- **STUB.** Running it fails with implementation guidance. M10 needs:
  1. REST PUT to `/databases/<db>/subscriptions` to create the sub (pure Ansible).
  2. A Python consumer using the RavenDB client (already installed via
     `ravendb_python_client_prerequisites`) that subscribes, logs each delivery's id + CV to CSV,
     exits on time/count/signal.
  3. REST DROP + DELETE on cleanup.
- See the file header for the spec.

### `diagnostic_partition_list.yml` (`toolbox/diagnostic/`)

- Read-only by design. SSH variant uses `become: true` only for `iptables -S` (root requirement);
  no other state changes.

### `lab_backups` docker volume

- Created by `provision_nodes.yml`. Removed by `teardown_containers.yml` alongside the containers and `captures/`.

---

## Design decisions (why the next maintainer will ask)

### Why `partition_set.yml` shells out to `cut_link.yml` instead of duplicating iptables logic

It does -- via `ansible-playbook` shell-out (same pattern as `force_cluster_asymmetry.yml` uses
with `upgrade_node.yml`). The added value is mode-aware dispatch (`cut_link.yml` vs
`cut_link_ssh.yml`) plus per-pair TCP-connect validation after rules apply.

### Why `wait_for_quiescence.yml` checks `DatabaseChangeVector` equality instead of etag stability

CV equality is RavenDB's actual replication-consistency primitive. Two nodes have the same data
exactly when their DB CVs are equal (modulo entry order). "Etag stopped moving for N seconds" is
an indirect inference; CV equality is the direct check.

### Why `set_mentor_node.yml` reads the existing task and `combine()`s instead of building a body from scratch

Each task type's PUT body has its own required fields (Backup needs `BackupType` +
`LocalSettings`, ETLs need connection-string refs, etc.). GET-modify-PUT means we never have to
know what those fields are -- we just preserve whatever is there and tweak `MentorNode`.

### Why the `diagnostic_` / `wait_for_` prefixes (Option B was picked)

Mutators are the default tool type -- prefixing the majority bucket adds noise without info.
Read-only and sync-blocking are the exceptions and benefit from the prefix because they tell you
"this won't change state" / "this will block until X" before you open the file.

### Why subdirectories under `toolbox/` (Option A grouping)

The README's toolbox table grouped tools this way anyway. Flat `toolbox/` made `ls toolbox/` an
opaque wall of files. Subdirs (`network/`, `db/`, `writes/`, `tasks/`, `backup/`, `subscriptions/`,
`service/`, `diagnostic/`, `wait/`) match the table, so a user browsing the filesystem sees the
same structure as the docs.

### Why no `partition_set_ssh.yml` (and same for `heal_all`, `diagnostic_partition_list`)

These tools branch on `target_mode` internally and shell out to the right underlying primitive
(`cut_link.yml` vs `cut_link_ssh.yml`). A separate `_ssh` variant would just be a duplicate of
the mode check.

---

## EMR-scenario gotchas (learned the hard way)

A grab-bag of "we hit this more than once" footguns from authoring RV-1 / RP-1 / RPV-1.

### `vars.yml` is NOT reachable from `import_playbook:` `vars:` blocks

Ansible evaluates `import_playbook` vars at parse time, before any role / playbook-level
`vars_files:` directive has loaded the scenario's `vars.yml`. Symptom: a var defined in
`scenarios/EMR/<X>/vars.yml` evaluates to `Undefined` inside the importing playbook's
`vars:` dict, but works fine inside the imported playbook's tasks.

Fix pattern — duplicate the default inline at every import call site using `| default(...)`:

```yaml
- import_playbook: ../../../toolbox/waits/wait_for_quiescence.yml
  vars:
    targets: "{{ hub_cluster_nodes | default([hub_id|default(1)~'a', hub_id|default(1)~'b', hub_id|default(1)~'c']) }}"
    budget_secs: "{{ quiesce_budget_secs | default(1200) }}"
```

The scenario `vars.yml` files document this in a comment header. Same bug class shows up
for `peer_map`, `upgrade_step_N`, `size_baseline_file`, `phase3_doc_id`.

### `playbook_dir` resolves to the IMPORTED playbook, not the importer

Inside an imported `toolbox/*.yml`, `{{ playbook_dir }}` is the toolbox dir — not the
scenario dir that called `import_playbook`. When you need a path relative to the scenario
fixtures, don't use `playbook_dir`. Use either an absolute path passed in via `-e`, or
`{{ lookup('env', 'PWD') }}/scenarios/EMR/<X>/...` (assumes the run.sh `cd`s to repo root,
which all our run.sh scripts do).

This burned `partition_set.yml`'s `chdir:` once too — that one is now `playbook_dir/../..`
(toolbox/network → repo root) instead of `playbook_dir/..`.

### `declare -A` requires `executable: /bin/bash`

Ansible's `shell` / `command` module defaults to `/bin/sh` (dash on Ubuntu), which doesn't
have associative arrays. Anything using `declare -A` (e.g. `wait_for_etag_parity.yml`'s
per-node-stability loop) MUST set:

```yaml
- shell: |
    declare -A first second
    ...
  args:
    executable: /bin/bash
```

Same applies to `[[ ... ]]`, `<<<` here-strings, `${var,,}` lowercase, process substitution.

### `LastDatabaseEtag` is per-node-local, not cross-node-parity-equal

A natural intuition is "wait until every node reports the same etag" — but
`LastDatabaseEtag` is a per-node writer counter; replicated writes don't carry their
source-node etag with them. The spec-aligned check is per-node **stability**:

1. Sample each node's etag, record into a map.
2. Sleep 3–5 s.
3. Sample again. Drained = every node's etag unchanged between samples.

Implemented in `toolbox/wait/wait_for_etag_parity.yml` (despite the name, it's
per-node-stability — name kept for tool-set symmetry).

For cross-node convergence use `DatabaseChangeVector` equality
(`wait_for_quiescence.yml`) — that one IS designed to converge.

### `/stats` fields that drift forever between nodes (per-node-intrinsic)

These three are NOT cross-node parity invariants — they reflect per-node-local storage
state and do not converge:

- `CountOfCounterEntries` (per upstream guidance, 2026-06-04; deterministic in our T3 lab — 1a always
  trails ~2-5 entries on RPV-1)
- `CountOfTimeSeriesSegments` (per same upstream note)
- `SizeOnDisk` (always — voron compaction is per-node-async)

`diagnostic_stats_parity.yml` flags these as `drift (info)` in a separate
`INFORMATIONAL DRIFT` banner block but does NOT fail the run. If a future test grows
parity asserts here, it has to opt out of these three fields explicitly.

### Tombstone-cleanup convergence needs its own wait

After a workload that does deletes / revert-from-revision stops, `CountOfTombstones`
will drift between nodes until each node's background cleanup runs. Etag stability says
"no new writes" but tombstone counts only equalize once cleanup catches up. We added
`wait_for_stats_field_parity.yml` (300s default budget) and call it after every workload
stop in RV-1 sections 7 / 10 / 12 and the RPV-1 section 12 endpoint.

### TCP TIME_WAIT exhaustion after curl bursts

W-3 and the RP-1 burst step do thousands of short-lived curl calls in a tight loop.
Ephemeral ports go to TIME_WAIT and the next /stats sweep fails with
`Cannot assign requested address`. The scenarios already insert `w3_cooldown_secs` /
`burst_cooldown_secs` (default 45 s) pauses after the burst step. If your kernel's
ephemeral port range is small, raise the cooldown or `sysctl -w net.ipv4.tcp_tw_reuse=1`
on the controller.

### `jq` is a hard controller dependency for W-3 and RP-1

`workload_w3.sh` does the revert-from-revision pattern by GETting the prior revision's
body, stripping `@metadata`, and PUTting it back — that requires `jq`. Same for the RP-1
burst step that re-PUTs revision payloads. Install with `apt install jq` on the
controller. The workloads exit early with a clear `ERROR: jq is required` if it's
missing, but the failure mode is "tests die mid-section", not "graceful skip".

### Workloads run indefinitely until explicitly killed

Per the EMR plan ("continuous from T0 through endpoint") all W-* workloads default to
`DURATION_SECS=0` (the indefinite sentinel) and only stop when the scenario invokes
`toolbox/workloads/stop_workload.yml`. **Never** set `duration_secs` to a finite value
in a scenario unless that section explicitly wants a fixed-window write phase.

The companion `assert_workload_alive.yml` should be called immediately BEFORE every
`stop_workload` to catch silently-died workers — otherwise `stop_workload` shrugs
("WORKLOAD ALREADY EXITED") and the run passes with degraded load coverage.

### Parallel runs on one docker daemon need disjoint cluster ids AND networks

Container names are globally unique per daemon. To run RV-1 + RP-1 + RPV-1 in three
shells on the same host, each `run.sh` invocation needs:

- a **disjoint** `cluster_id_start` range (RV-1 takes 1; RP-1 takes 4–5; RPV-1 takes 7–9),
- a **unique** `docker_network_name` (`rv1net` / `rp1net` / `rpv1net`).

Teardown is scoped per-network so they don't stomp each other on cleanup. See
`scenarios/EMR/README.md` for the canonical three-shell pattern.
