# ansible-ravendb-chaos

A small ansible harness that spins up N docker containers, installs RavenDB on each, and merges them into clusters of 3 (or whatever you set). Use it to chaos-test cluster behaviour locally - kill a node, partition a network, see what happens.

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

The `ravendb.ravendb` ansible collection ship with the standard ansible distribution, so you don't have to install it separately (we are included yay <3 ).

### Linux / WSL2

```bash
sudo apt update
sudo apt install -y ansible python3 python3-pip docker.io openssl
```

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
ansible-playbook playbooks/provision_nodes.yml -e clusters_count=3

# 2. install RavenDB on every container, register the admin cert on each leader
ansible-playbook playbooks/install_ravendb.yml

# 3. merge each cluster's nodes into one RavenDB cluster
#    (-K because it edits the controller's /etc/hosts)
ansible-playbook playbooks/form_clusters.yml -K
```

At the end of step 3 you get a summary like:

```
  Cluster 1
    tag   container   url
    A     1a          https://1a.hubsink.test:443
    B     1b          https://1b.hubsink.test:443
    C     1c          https://1c.hubsink.test:443

  Cluster 2
    ...
```

Studio: open `https://1a.hubsink.test/studio` in a browser.

### Teardown

```bash
ansible-playbook playbooks/teardown_nodes.yml -K
```

Removes every container on the network, removes the network, strips the `/etc/hosts` block.

---

## What each playbook does, briefly

| playbook | does |
|---|---|
| `provision_nodes.yml` | creates the shared docker network (`hubsinknet`), launches one privileged systemd-ready ubuntu container per `<cluster_id><node_letter>` |
| `install_ravendb.yml` | discovers containers from the network, trusts the CA on the controller, runs the `ravendb.ravendb.ravendb_node` role on every container (installs the deb, deploys the server cert, configures self-signed TLS), then registers `client.pfx` as the admin certificate **only on cluster leaders** (intentional - registering on followers commits them to their own cluster topology and blocks the merge) |
| `form_clusters.yml` | writes `<name>.hubsink.test -> container ip` into `/etc/hosts` on the controller and on every container, then for each cluster joins the non-leader nodes to the leader using the `ravendb.ravendb.node` ansible module |
| `teardown_nodes.yml` | the reverse of provision + strips `/etc/hosts` |

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

---

## Things that bite

- **CA trust on Linux Chrome.** Chrome on Linux reads its own NSS db, not the system trust store. `update-ca-certificates` on the controller fixes curl, not the browser. To fix the browser, use Firefox or import `ca.crt` into NSS via `certutil` (`apt install libnss3-tools` then `certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "Hubsink Test Root CA" -i ca.crt`).
- **Don't put the project under `/mnt/c` on WSL2.** Ansible will ignore your `ansible.cfg` because the dir is world-writable. Move it to `~/...` or `export ANSIBLE_CONFIG=$PWD/ansible.cfg`.
- **Pulled `clusters_count` down and re-running install** - the old containers from the previous bigger run are still there. Run teardown first.
- **Free RavenDB cluster size is capped.** A community/free license maxes out at 3 cluster nodes. Use a Developer license for more.
