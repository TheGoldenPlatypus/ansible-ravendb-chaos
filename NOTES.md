# Notes, caveats, design decisions

The stuff that doesn't belong in the main README but a future maintainer will want.

---

## Environment caveats

### WSL2 + Hyper-V wedge (SSH-mode chaos)

Driving SSH-mode chaos from WSL2 is unreliable.  WSL2 with `networkingMode=mirrored` shares the
Windows host's network through a Hyper-V virtual switch; Windows's stateful filter sits in the
path.  When iptables chaos fires (docker REJECT or SSH DROP), the resulting TCP failure patterns
cause Windows to silently wedge subsequent WSL→VM TCP.  **Symptom:** SSH from WSL to the cut VMs
hangs forever (ICMP ping still works), even though iptables on the VMs has nothing blocking the
controller's source IP.

**Fix:** drive chaos scenarios from a non-WSL Linux controller.  The toolbox itself works fine
on WSL for docker-mode; the wedge only bites SSH-mode chaos.

### WSL2 kernel limits for many parallel containers

WSL2 defaults are tight: `kernel.pid_max=32768`, `fs.inotify.max_user_watches=8192`.  Running
multiple company-1 scenarios in parallel (each spawns 3-9 privileged systemd containers, each forks
~80 processes) overshoots these defaults and surfaces as random `OCI runtime exec failed: ...
setns process: exit status 1` during install.  Bump them once:

```bash
sudo tee /etc/sysctl.d/99-ravendb-chaos.conf <<'EOF'
kernel.pid_max = 4194304
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 8192
EOF
sudo sysctl --system
```

### Hardware capacity

Chaos scenarios assume RavenDB restarts in seconds.  On nodes with < 1 GB RAM (e.g. Raspberry
Pi Zero), `systemctl restart ravendb` can take ~50s+ and timing-sensitive scenarios won't
behave usefully.  **Aim for ≥ 2 GB RAM per node for real testing.**

### company-1 scenarios are docker-only

The company-1 scenarios under `scenarios/company-1/` assume containers on a single docker daemon (via
`community.docker`).  Some toolbox tools have `_ssh` variants for bare-metal VM runs
(`cut_link_ssh`, `partition_node_ssh`, `heal_node_ssh`), but the company-1 scenarios themselves
don't wire ssh mode -- and several newer primitives (`inject_egress_delay`,
`clear_egress_delay`, `hard_kill_ravendb`, `mutate_sink_filter`) are docker-only with no
ssh variant.

### SSH per-host `/backups` (no shared FS)

`setup_ssh_targets.yml` creates `/backups` on each VM with `0777` perms (parity with docker's
`/backups`), but **every VM has its own local `/backups`** -- there's no shared filesystem.

- Same-host backup→restore: works out of the box.
- Cross-host restore: `scp -r` the backup folder between hosts yourself.

The backup/restore tools are mode-agnostic at the REST level -- only docker happens to provide
a shared `lab_backups` volume.

---

## Tool-specific quirks

### `write_timeseries.yml` / `ravendb_writes kind=timeseries`

- Append mode shells out to `date -u -d` once per entry.  **Slow for large `count`** (~10ms per
  entry).  Acceptable for chaos seeding (count ≤ 100); not for stress runs.
- Delete-range mode is **inclusive on both bounds** -- `[from, to]`, not `[from, to)`.  RavenDB
  semantics, not ours.

### `write_attachments.yml`, `write_counters.yml`

- Both require the parent doc to exist.  Run `write_docs` first or PUT the doc via curl.

### `write_docs.yml` (single-rev mode)

- PUTs the same fixed body every call.  **RavenDB dedups identical-content PUTs into a single
  revision.**  For distinct revisions use `write_docs_revisions` (`ravendb_writes
  kind=docs_revisions`) which emits a unique body per PUT.

### `restore_backup.yml` / `ravendb_backup kind=restore`

- `backup_path` is the **folder** containing the `.ravendb-snapshot` (or `.ravendb-backup`)
  file, not the file itself.  RavenDB nests by `<db>-<tag>-snapshot/<timestamp>.ravendb-snapshot`:

  ```bash
  docker exec 1a find /backups/snap-test -name '*.ravendb-snapshot' -printf '%h\n' | head -1
  # /backups/snap-test/2026-05-28-18-13-36.ravendb-Tenants-A-snapshot   ← pass THIS as backup_path
  ```

### `open_subscription.yml` (`toolbox/subscriptions/`)

- **STUB.**  Running it fails with implementation guidance.  Needs:
  1. REST PUT to `/databases/<db>/subscriptions` to create the sub (pure Ansible).
  2. A Python consumer using the RavenDB client that subscribes, logs each delivery's id + CV
     to CSV, exits on time/count/signal.
  3. REST DROP + DELETE on cleanup.

### `set_mentor_node` for non-pull-replication task types

- Wired for `task_type=hub`, `sink`, `external`.  Seven other task types (`backup`,
  `subscription`, `raven_etl`, `sql_etl`, `olap_etl`, `elastic_etl`, `queue_etl`) need explicit
  GET-modify-PUT handling per their per-task body shape.

---

## Design decisions

### Toolbox YAML wrappers stayed as examples after the module migration

`library/ravendb_*` modules replaced the YAML-task-loop implementations in many `toolbox/*.yml`
wrappers, but the wrappers stayed.  They serve two purposes: (1) CLI runnability for
exploratory work (`ansible-playbook toolbox/writes/write_docs.yml -e target=1a -e ...`), and
(2) examples of the canonical parameter shape per module.  Scenarios call the modules directly
inside `tasks:` blocks; wrappers are not in the scenario hot path.

### `wait_for_etag_parity` is per-node-stability, not cross-node-equality

`LastDatabaseEtag` is a per-node writer counter; replicated writes don't carry their source
etag.  The drained check is: sample each node's etag, sleep ~3-5s, sample again — drained =
every node's etag is unchanged between samples.  For cross-node convergence use
`DatabaseChangeVector` equality (`ravendb_wait kind=quiescence`).

### `diagnostic_` / `wait_for_` prefixes (mutators are unprefixed)

Mutators are the default tool type -- prefixing the majority bucket adds noise without info.
Read-only and sync-blocking are the exceptions and benefit from the prefix because they
identify "this won't change state" / "this will block" at a glance.

---

## company-1-scenario authoring gotchas

The footguns that bit us writing RV-1 / RP-1 / RP-2 / RPV-1 — useful when adding the remaining
scenarios (RP-3 / RV-2 / RV-3 / RPV-2).

### `vars.yml` is NOT reachable from `import_playbook:` `vars:` blocks

Ansible evaluates `import_playbook` vars at parse time, before any `vars_files:` directive has
loaded the scenario's `vars.yml`.  Symptom: a var defined in `scenarios/company-1/<X>/vars.yml`
evaluates to `Undefined` inside the importing playbook's `vars:` dict, but works fine inside
the imported playbook's tasks.

Fix pattern -- duplicate the default inline at every import site using `| default(...)`:

```yaml
- import_playbook: ../../../toolbox/wait/wait_for_etag_parity.yml
  vars:
    nodes: "{{ hub_cluster_nodes | default([hub_id|default(1)~'a', ...]) }}"
    timeout_secs: "{{ quiesce_budget_secs | default(1200) }}"
```

Same bug class shows up for `peer_map`, `upgrade_step_N`, `size_baseline_file`, `phase3_doc_id`.

### `ansible.builtin.command: ansible-playbook ...` does NOT inherit `-e` overrides

When a scenario spawns an inner playbook via `command:` (instead of `import_playbook:`), the
inner process is a fresh ansible-playbook with no extra-vars from the parent.  All overrides
the user passed at the top-level run.sh (`docker_network_name`, `cluster_id_start`,
`replication_certs_dir`, etc.) MUST be explicitly re-passed:

```yaml
- ansible.builtin.command:
    cmd: >-
      ansible-playbook toolbox/network/partition_set.yml
      -e docker_network_name={{ docker_network_name | default('hubsinknet') }}
      -e '{"set_a": [...], "set_b": [...]}'
```

`import_playbook` does inherit -- use it when possible.  `command` is only needed when you have
to run an inner playbook from inside a play (`import_playbook` is top-level only).

### `playbook_dir` resolves to the IMPORTED playbook, not the importer

Inside an imported `toolbox/*.yml`, `{{ playbook_dir }}` is the toolbox dir, not the scenario
dir.  When you need a path relative to scenario fixtures, use either an absolute path passed via
`-e`, or `{{ lookup('env', 'PWD') }}/scenarios/company-1/<X>/...` (assumes the run.sh `cd`s to repo
root, which all our run.sh scripts do).

### Background workloads — launch the .sh directly, not via `ansible-playbook`

The historical pattern `nohup ansible-playbook scenarios/company-1/workloads/wN/workload_wN.yml &`
spawns 5 process levels (outer ansible → bash subshell → nohup → inner ansible-playbook →
.sh → curl).  A signal anywhere up the chain can kill the .sh and the EXIT trap removes the
pidfile — `assert_workload_alive` then trips with `NO_PIDFILE` and the scenario fails for the
wrong reason.

Canonical launcher is now:

```yaml
- ansible.builtin.command:
    cmd: "bash {{ playbook_dir }}/../workloads/wN/workload_wN.sh"
  environment:
    TARGET: "{{ hub_id | default(1) }}a"
    DB_NAME: "{{ db_name }}"
    ...
  async: 14400  # 4h backstop; stopped via stop_workload
  poll: 0
```

Two process levels (Ansible async wrapper → .sh).  Survives controller signal storms.

### Workload curl needs `--connect-timeout` + `--max-time`

Without timeouts, a curl in W-1/W-2/W-5 can hang for 75s+ on a node mid-restart (container
recreate window).  In bursty multi-curl loops this puts the .sh in a bad state that ends in
EXIT — pidfile gone, `assert_workload_alive` fails for the wrong reason.

All workload `.sh` files now use `curl --connect-timeout 5 --max-time 15` plus `|| echo 000` so
a curl failure always emits a status code the wrapper can parse.

### `declare -A` requires `executable: /bin/bash`

Ansible's `shell` / `command` defaults to `/bin/sh` (dash on Ubuntu), which doesn't have
associative arrays.  Anything using `declare -A`, `[[ ... ]]`, `<<<` here-strings, `${var,,}`
lowercase, or process substitution must set:

```yaml
- shell: |
    declare -A first second
    ...
  args:
    executable: /bin/bash
```

### `/stats` fields that drift forever between nodes (per-node-intrinsic)

These three are NOT cross-node parity invariants -- they reflect per-node-local storage state
and do not converge:

- `CountOfCounterEntries` (per upstream guidance, 2026-06-04)
- `CountOfTimeSeriesSegments` (per same upstream note)
- `SizeOnDisk` (always -- voron compaction is per-node-async)

`ravendb_diagnostic kind=stats_parity` flags these as `drift (info)` in a separate
`INFORMATIONAL DRIFT` banner block but does NOT fail the run.

### Tombstone-cleanup convergence needs its own wait

After deletes / revert-from-revision stops, `CountOfTombstones` drifts between nodes until
background cleanup runs.  Etag stability says "no new writes" but tombstone counts only
equalize once cleanup catches up.  Call `ravendb_wait kind=stats_field_parity
fields=[CountOfTombstones]` (300s default budget) after every workload stop.

### TCP TIME_WAIT exhaustion after curl bursts

W-3 and the RP-1 burst step do thousands of short-lived curl calls in a tight loop.  Ephemeral
ports go to TIME_WAIT and the next `/stats` sweep fails with `Cannot assign requested address`.
Scenarios insert a `w3_cooldown_secs` / `burst_cooldown_secs` pause (default 45s) after burst
steps.  If your kernel's ephemeral port range is small, raise the cooldown or `sysctl -w
net.ipv4.tcp_tw_reuse=1` on the controller.

### `jq` is a hard controller dependency for W-3 and RP-1

`workload_w3.sh` does revert-from-revision by GETting the prior revision's body, stripping
`@metadata`, and PUTting it back -- that requires `jq`.  Same for the RP-1 burst step.
`apt install jq` on the controller.

### Workloads run indefinitely until explicitly killed

Per the company-1 plan ("continuous from T0 through endpoint") all W-* workloads default to
`DURATION_SECS=0` (the indefinite sentinel) and only stop when the scenario invokes
`stop_workload.yml`.  **Never** set `duration_secs` in a scenario unless that section
explicitly wants a fixed-window write phase.

Call `assert_workload_alive.yml` immediately before every `stop_workload` to catch
silently-died workers; without it `stop_workload` shrugs "WORKLOAD ALREADY EXITED" and the run
passes with degraded load.  The assert now also dumps the worker's debug log (`/tmp/wN-...
debug.log`) on failure so you can see signal / last op / heartbeat history.

### Parallel runs on one docker daemon need disjoint cluster ids AND networks

Container names are globally unique per daemon.  To run several scenarios in parallel each
`run.sh` invocation needs:

- a **disjoint** `cluster_id_start` range, AND
- a **unique** `docker_network_name`.

Suggested staggering: RV-1 takes cid 1; RP-1 takes 2-3; RP-2 takes 4-6; RPV-1 takes 7-9.
Teardown is scoped per-network so they don't stomp each other on cleanup.
