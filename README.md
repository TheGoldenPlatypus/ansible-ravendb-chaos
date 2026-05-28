# ansible-ravendb-chaos

## A small ansible harness that spins up N docker containers, installs RavenDB on each, and merges them into clusters of 3 (or whatever you set). Use it to chaos-test cluster behaviour locally - kill a node, partition a network, restart a service, see what happens.

<p align="center">
  <img src="assets/banner2.png" alt="ansible-ravendb-chaos" width="1600">
</p>

---

## What ansible is (30-second version)

Ansible reads YAML files called **playbooks** and runs the tasks in them against one or more **hosts**. Hosts can be remote machines over SSH, your local machine, or - what we do here - docker containers (it `docker exec`s into them).

You run `ansible-playbook some.yml` and it does its thing.

---

## What you need installed

| tool | why |
|---|---|
| docker | the containers |
| python 3 | ansible runs on python |
| ansible (>= 2.15) | the playbook runner |

The `ravendb.ravendb` ansible collection ships with the standard ansible distribution, so you don't have to install it separately (we are included yay <3 ).

### Linux / WSL2

```bash
sudo apt update
sudo apt install -y ansible python3 python3-pip docker.io openssl
```

<details>
<summary>If you don't already have docker / python3 / ansible installed (click to expand)</summary>

#### docker

```bash
sudo apt update
sudo apt install -y docker.io
sudo systemctl enable --now docker

# let your user run docker without sudo
sudo usermod -aG docker $USER
# log out + back in (or run `newgrp docker`) for the group change to take effect

# sanity check
docker run --rm hello-world
```

#### python 3

```bash
sudo apt install -y python3 python3-pip
python3 --version    # should be >= 3.9
```

#### ansible

```bash
sudo apt install -y ansible
ansible --version    # should be >= 2.15
```

If your distro's ansible is too old, install via pip instead:

```bash
python3 -m pip install --user "ansible-core>=2.15"
```

</details>

### Windows

Running ansible directly on Windows is painful. Install **WSL2 + Docker Desktop** (toggle "Use WSL 2 based engine" in Docker Desktop settings), install Ubuntu from the Microsoft Store, and follow the Linux steps inside the Ubuntu shell. Docker Desktop on Windows runs the containers through the WSL2 engine, so everything you do from Ubuntu reaches them.

Keep the project under `~/...` inside WSL, not `/mnt/c/...`. Files under `/mnt/c` are world-writable from ansible's point of view, which makes it ignore `ansible.cfg`.

---

## Certs and license

You don't generate the TLS material. Four pre-built cert files (`ca.crt`, `ca.key`, `server.pfx`, `client.pfx`) live in a shared drive folder:

> https://drive.google.com/file/d/1frqQp_3ZeSvoDfTBhj8YoSO6XgFc76q8/view?usp=sharing

You **do** need to bring your own `license.json` - grab one from your RavenDB account and drop it next to the four cert files.

So in the end your local folder should contain five files:

```
ca.crt
ca.key
server.pfx
client.pfx
license.json
```

The default path the playbooks look in is `/mnt/c/dev/hub-sink/selfsignedmaterials/`. If you put the folder somewhere else, override `cert_dir` in `inventory/group_vars/all.yml`.

---

## How the layout works

Every container's docker name is `<cluster_id><node_letter>`:

| cluster_id | nodes |
|---|---|
| 1 | 1a, 1b, 1c |
| 2 | 2a, 2b, 2c |
| 3 | 3a, 3b, 3c |
| ... | ... |

- `cluster_id` is just an incrementing integer.
- Node letters come from the fixed alphabet `a..z`, so the hard cap is 26 nodes per cluster.
- The node ending in `a` is always the cluster's leader (the one the others join into).
- Inside each cluster the node tags shown in RavenDB Studio are `A`, `B`, `C`, ...
- The hostname every node announces to its peers (`PublicServerUrl`) is `https://<container_name>.hubsink.test:443`.

The number of clusters and the number of nodes per cluster are the two knobs. Defaults in `inventory/group_vars/all.yml`:

```yaml
clusters_count: 1
nodes_per_cluster: 3
```

Override on the command line:

```bash
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=5 -e nodes_per_cluster=4
```

That gives you 5 independent clusters x 4 nodes each = 20 containers. Each cluster is self-contained; cluster 1 doesn't know about cluster 2.

---

## Running it

Four playbooks, run in order:

```bash
# 1. spin up the containers
#    e.g. 3 clusters x 3 nodes each = 9 containers named 1a/1b/1c, 2a/2b/2c, 3a/3b/3c
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=3 -e nodes_per_cluster=3

# 2. install RavenDB on every container, register the admin cert on each leader
ansible-playbook playbooks/install_ravendb.yml

# 3. merge each cluster's nodes into one RavenDB cluster
#    (-K because it edits the controller's /etc/hosts)
ansible-playbook playbooks/form_clusters.yml -K
```

At the end of step 3 you get a summary like:

```
TASK [Print the final cluster layout (tag / container / url per cluster)]
*************************************************************************
ok: [localhost] =>
  msg:
================================================================
  Formed 3 cluster(s) with 9 node(s) on hubsinknet
================================================================

Cluster 1
  tag   container   url
  A     1a          https://1a.hubsink.test:443
  B     1b          https://1b.hubsink.test:443
  C     1c          https://1c.hubsink.test:443

Cluster 2
  tag   container   url
  A     2a          https://2a.hubsink.test:443
  B     2b          https://2b.hubsink.test:443
  C     2c          https://2c.hubsink.test:443

Cluster 3
  tag   container   url
  A     3a          https://3a.hubsink.test:443
  B     3b          https://3b.hubsink.test:443
  C     3c          https://3c.hubsink.test:443

================================================================
```

Studio: open `https://1a.hubsink.test/studio` in a browser.

### Teardown

```bash
ansible-playbook playbooks/teardown_containers.yml -K
```

Removes every container on the network, removes the network, strips the `/etc/hosts` block.

---

## SSH mode (VMs / bare metal / other computers)

Everything above runs against docker containers by default. The same harness also runs against
real machines reachable over SSH - useful for lab VMs, bare-metal servers, cloud instances,
or any persistent host rather than an ephemeral container.

### Switching modes

`target_mode` is set by the inventory file you pass. There is no environment switch - passing
`-i inventory/ssh_hosts.yml` selects ssh mode; running with no `-i` (or with a docker inventory)
selects docker mode. The setup/install/form playbooks and the read-only toolbox tools work in
either mode without code changes. **The network-chaos toolbox tools are split into two parallel
sets** - see "Network-chaos tools" below.

### Bring-up

```bash
# 1. copy the inventory template and fill in your hosts
cp inventory/ssh_hosts.yml.example inventory/ssh_hosts.yml
# edit inventory/ssh_hosts.yml -- set ansible_host (LAN IP) and ansible_user per host

# 2. verify SSH access + install apt prereqs (iptables, python3)
ansible-playbook -i inventory/ssh_hosts.yml playbooks/setup_ssh_targets.yml

# 3. install RavenDB on every host (~5-10 min, slowest step; pulls arm64 deb on aarch64)
ansible-playbook -i inventory/ssh_hosts.yml playbooks/install_ravendb.yml

# 4. merge them into a cluster (also writes /etc/hosts on the controller + each host)
ansible-playbook -i inventory/ssh_hosts.yml playbooks/form_clusters.yml
```

The hosts in `inventory/ssh_hosts.yml` follow the same `<cluster_id><letter>` convention as docker
(`1a`/`1b`/`1c`...). Scenarios and read-only toolbox tools reference these names - same CLI as docker.

### Network-chaos tools (`*_ssh.yml`)

The four iptables-based chaos primitives have **separate SSH variants** because the docker tools
run iptables *inside containers* (via `docker_container_exec`) and don't speak SSH at all:

| docker | ssh variant |
|---|---|
| `toolbox/cut_link.yml` | `toolbox/cut_link_ssh.yml` |
| `toolbox/restore_link.yml` | `toolbox/restore_link_ssh.yml` |
| `toolbox/partition_node.yml` | `toolbox/partition_node_ssh.yml` |
| `toolbox/heal_node.yml` | `toolbox/heal_node_ssh.yml` |

```bash
# example: cut 1a <-> 1b in ssh mode
ansible-playbook -i inventory/ssh_hosts.yml toolbox/cut_link_ssh.yml -e node_a=1a -e node_b=1b
```

Use `*_ssh.yml` when targets are inventory hosts. Use the docker-named variants when targets are
docker containers. They don't overlap - pick the one that matches your inventory.

Mechanism difference: SSH variants use `iptables -j DROP` instead of `REJECT --reject-with tcp-reset`.
DROP is silent (no TCP RST is emitted), which is more realistic for "the peer disappeared" chaos
and avoids triggering the WSL/Hyper-V caveat below.

### Teardown

```bash
ansible-playbook -i inventory/ssh_hosts.yml playbooks/cleanup_ssh_targets.yml
```

Uninstalls RavenDB via the role's `state=absent` (stops service, purges package, removes
`/usr/lib/ravendb`, `/var/lib/ravendb`, `/etc/ravendb`, `/var/log/ravendb`, removes ravendb user
and group). Also flushes leftover chaos iptables rules and strips the `/etc/hosts` block. The
machines themselves stay; only what the harness installed is removed.


### Things that bite in SSH mode

- **WSL/Hyper-V wedge - driving SSH-mode chaos from WSL2 is unreliable.**  
  WSL2 with `networkingMode=mirrored` shares the Windows host's network through a Hyper-V
  virtual switch. Windows's stateful filter sits in the path. When iptables-based chaos cuts
  fire (either docker-mode REJECT or SSH-mode DROP), the resulting TCP failure patterns cause
  Windows to silently wedge subsequent WSL→VM TCP. Symptom: SSH from WSL to the cut VMs hangs
  forever (ICMP ping still works), even though iptables on the VMs has nothing blocking the
  controller's source IP.
  
- **Hardware capacity**: chaos scenarios assume RavenDB restarts in seconds. On very small
  hardware (< 1 GB RAM) restarts can take ~50s+ and timing-sensitive scenarios won't behave
  usefully. Aim for ≥ 2 GB RAM per node for real testing.

---

## Custom RavenDB builds

By default `install_ravendb.yml` pulls the version from `rdb_version` (group_vars) off the official daily-builds bucket. To install a developer build instead - a branch artifact, a nightly, a local `.deb` - pass `custom_build` and skip the role's download step:

```bash
# from a URL (e.g. internal CI artifact)
ansible-playbook playbooks/install_ravendb.yml \
    -e custom_build=https://internal.example.com/raven-feature-branch.deb \
    --skip-tags download

# from a local file on the controller
ansible-playbook playbooks/install_ravendb.yml \
    -e custom_build=/home/me/raven-feature-branch.deb \
    --skip-tags download
```

The `--skip-tags download` is required - it tells the upstream role to leave `/tmp/ravendb.deb` alone (we pre-staged our file there) instead of wiping and re-fetching the official build.

Same flag is supported by `add_node.yml` (below) and `toolbox/upgrade_node.yml`.

---

## Adding a node later

`playbooks/add_node.yml` provisions ONE extra container and optionally joins it to an existing cluster. Use it to grow a cluster mid-test, add a standalone node with a different RavenDB version, or chaos-test "what happens when a node joins live."

Three real shapes:

```bash
# standalone bootstrapped 1-node cluster
ansible-playbook playbooks/add_node.yml -K -e node_name=solo1

# standalone PASSIVE -- not bootstrapped, ready to be added to a cluster manually via Studio
ansible-playbook playbooks/add_node.yml -K -e node_name=xyz3 -e passive=true

# join an existing cluster (here: grow cluster 1 with a 4th node)
ansible-playbook playbooks/add_node.yml -K -e node_name=1d -e join_to=1a
```

See the file's header for all six documented variants (custom build, explicit tag, non-convention names, etc.).

---

## Toolbox

Small, single-purpose playbooks under `toolbox/`. Each one is CLI-runnable on its own AND importable from a scenario playbook via `import_playbook`. Compose chaos scenarios by chaining them.

| playbook | what it does |
|---|---|
| `cut_link.yml` | (docker) REJECT all TCP between two containers (forces TCP reset, so existing connections die) |
| `restore_link.yml` | (docker) symmetric inverse |
| `partition_node.yml` | (docker) cut every link between one node and every cluster peer |
| `heal_node.yml` | (docker) symmetric inverse |
| `cut_link_ssh.yml` | (ssh) DROP all TCP between two inventory hosts (silent, no RST -- see "Things that bite") |
| `restore_link_ssh.yml` | (ssh) symmetric inverse |
| `partition_node_ssh.yml` | (ssh) cut every link between one inventory host and every cluster peer |
| `heal_node_ssh.yml` | (ssh) symmetric inverse |
| `restart_ravendb.yml` | `systemctl restart ravendb` + wait for HTTPS to come back |
| `write_docs.yml` | PUT N docs to a target node (single prefix) |
| `write_docs_interleaved.yml` | PUT N docs to a target node, round-robin across multiple id prefixes |
| `write_docs_freeform.yml` | PUT N freeform docs (random GUID id, null collection) -- useful for chaos writes with no predictable shape |
| `delete_docs.yml` | DELETE docs by explicit id-list or by id-prefix + count |
| `force_cluster_asymmetry.yml` | upgrade specific nodes to specific versions per a version-map (force the cluster into an asymmetric version state for backward-compat tests) |
| `read_doc_count.yml` | print `CountOfDocuments` from `/stats` |
| `create_database.yml` | create a database via the ravendb collection's `database` module |
| `delete_database.yml` | delete + poll until `/databases` no longer lists it |
| `remove_node.yml` | remove a node from its cluster (cluster admin API + verify) |
| `upgrade_node.yml` | upgrade (or downgrade) RavenDB on one container; supports `rdb_version` or `custom_build` |
| `show_replication.yml` | dump incoming + outgoing replication connections for a database |
| `wait_for_healthy.yml` | wrap `ravendb.ravendb.healthcheck` (currently `node_alive` + `cluster_connectivity` only) |
| `wait_for_rehab.yml` | block until a target node enters DB-level rehab (Promotables/Rehabs); fails if it doesn't |
| `wait_for_member.yml` | block until a target node is back as a full Member (not Promotable/Rehab) |

Each playbook validates its inputs, prints a one-line "what just happened" debug, and is idempotent where it makes sense. Headers in each file show the inputs and 1-3 run examples.

### Examples

```bash
# cut 1a <-> 1b, then put it back
ansible-playbook toolbox/cut_link.yml     -e node_a=1a -e node_b=1b
ansible-playbook toolbox/restore_link.yml -e node_a=1a -e node_b=1b

# partition 1c from the whole cluster
ansible-playbook toolbox/partition_node.yml -e target=1c

# rolling upgrade across a 3-node cluster, with health gate between each step
for n in 1a 1b 1c; do
  ansible-playbook toolbox/upgrade_node.yml -e target=$n -e rdb_version=7.2.3
  ansible-playbook toolbox/wait_for_healthy.yml \
      -e cluster_leader=1a -e checks=node_alive,cluster_connectivity
done

# write 50 docs to 1a, then verify they landed on every node
ansible-playbook toolbox/create_database.yml -K -e cluster_leader=1a -e db_name=Tenants
ansible-playbook toolbox/write_docs.yml      -e target=1a -e db_name=Tenants -e count=50
for n in 1a 1b 1c; do
  ansible-playbook toolbox/read_doc_count.yml -e target=$n -e db_name=Tenants
done
```

---

## What each playbook does, briefly

| playbook | does |
|---|---|
| `provision_nodes.yml` | (docker) creates the shared docker network (`hubsinknet`), launches one privileged systemd-ready ubuntu container per `<cluster_id><node_letter>` |
| `setup_ssh_targets.yml` | (ssh) verifies SSH reachability + apt prereqs (iptables, python3) on every host in the inventory's `ravendb_nodes` group; SSH-mode equivalent of `provision_nodes.yml` (the hosts already exist; you brought them) |
| `install_ravendb.yml` | (both modes) discovers targets, trusts the CA on the controller, runs the `ravendb.ravendb.ravendb_node` role on every node (installs the deb, deploys the server cert, configures self-signed TLS, picks arm64 deb on aarch64), then registers `client.pfx` as the admin certificate **only on cluster leaders** (intentional - registering on followers commits them to their own cluster topology and blocks the merge). Supports `custom_build` for dev artifacts. |
| `form_clusters.yml` | (both modes) writes `<name>.hubsink.test -> ip` into `/etc/hosts` on the controller and on every node (disables cloud-init's `manage_etc_hosts` first in ssh mode so the block persists across reboots), then for each cluster joins the non-leader nodes to the leader using the `ravendb.ravendb.node` ansible module |
| `add_node.yml` | (docker only) adds a single extra container; can join it to an existing cluster, stay passive for manual join later, or come up as its own standalone 1-node cluster; supports `custom_build` |
| `teardown_containers.yml` | (docker) reverse of provision + strips `/etc/hosts` |
| `cleanup_ssh_targets.yml` | (ssh) uninstalls RavenDB via the role's `state=absent` on each host, flushes leftover chaos iptables rules, strips the `/etc/hosts` block. Hosts themselves stay. |
| `toolbox/*.yml` | small chaos primitives - cut/restore/partition/heal/restart/read/write/etc. - see the table above |

Everything is idempotent. Re-running a playbook is safe.

---

## Tweaking things

Everything tweakable lives in `inventory/group_vars/all.yml`:

| var | meaning |
|---|---|
| `clusters_count` | number of independent clusters to create |
| `nodes_per_cluster` | number of nodes per cluster (1..26) |
| `docker_network_name` | docker network name |
| `docker_image` | container image |
| `ravendb_domain` | domain used in every node's PublicServerUrl |
| `rdb_version` | RavenDB version to install |
| `cert_dir` | folder containing the cert/license files |

For one-off overrides, pass `-e key=value` on the command line. Same flag works for any input listed in a playbook's header.

---
