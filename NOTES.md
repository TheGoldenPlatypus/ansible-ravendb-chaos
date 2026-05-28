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
- Cross-host restore (Karmel's M11/P7 on VMs): you must copy the backup folder between hosts
  yourself:
  ```bash
  scp -r worker4@192.168.100.14:/backups/<folder> /tmp/relay
  scp -r /tmp/relay worker5@192.168.100.15:/backups/<folder>
  ```

The backup/restore tools are mode-agnostic at the REST level -- only the docker setup happens to
provide a shared `lab_backups` volume.

---

## Tool-specific quirks

### `wait_for_quiescence.yml` (`toolbox/wait/`)

- Ends the play via `meta: end_play` on convergence.
- **Do NOT `import_playbook` it into a larger play** -- the importer's remaining tasks would be
  cut off the moment quiescence fires. Safe as a standalone `ansible-playbook` shell-out, which
  is how scenarios should use it.
- Drops nodes returning 404/500/503 from the convergence set on the first poll (those don't have
  the DB). Mid-run transient failures don't abort the run.
- Carries state across iterations via `set_fact` + an `include_tasks` helper
  (`_wait_for_quiescence_attempt.yml`). The "X/90 attempts" iterations enumeration before any of
  them run is an Ansible 2.17 cosmetic wart (`break_when` on loops landed in 2.18); functionally
  the play ends correctly on the first converged attempt.

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

- Created by `provision_nodes.yml`. **Survives `teardown_containers.yml`.** Nuke separately:
  ```bash
  docker volume rm lab_backups
  ```

---

## Design decisions (why the next maintainer will ask)

### Why `partition_set.yml` shells out to `cut_link.yml` instead of duplicating iptables logic

It does -- via `ansible-playbook` shell-out (same pattern as `force_cluster_asymmetry.yml` uses
with `upgrade_node.yml`). The added value is mode-aware dispatch (`cut_link.yml` vs
`cut_link_ssh.yml`) plus per-pair TCP-connect validation after rules apply.

### Why `wait_for_quiescence.yml` checks `DatabaseChangeVector` equality instead of etag stability

CV equality is RavenDB's actual replication-consistency primitive. Two nodes have the same data
exactly when their DB CVs are equal (modulo entry order). "Etag stopped moving for N seconds" is
an indirect inference; CV equality is the direct check, and it's what every Karmel scenario
asserts as the headline invariant anyway.

### Why `set_mentor_node.yml` reads the existing task and `combine()`s instead of building a body from scratch

Each task type's PUT body has its own required fields (Backup needs `BackupType` +
`LocalSettings`, ETLs need connection-string refs, etc.). GET-modify-PUT means we never have to
know what those fields are -- we just preserve whatever is there and tweak `MentorNode`.

### Why no `workload_mixed.yml`

Karmel's plan referenced `tools/qa/filtered-workload.ps1` -- a script, not a playbook. Real
sustained rate-limited concurrent writers with CSV logging are easy in Python and miserable in
Ansible. Atomic writers (`write_docs`, `write_attachments`, `write_counters`, `write_timeseries`)
cover everything Karmel actually asserts; "mixed at sustained rate" was an aesthetic preference,
not a test invariant.

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
