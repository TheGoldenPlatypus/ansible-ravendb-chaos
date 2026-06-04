#!/usr/bin/env bash
###################################################################################################
# scenarios/EMR/RP1/run.sh -- run the full RP-1 test end-to-end.
#
# Teardown -> provision T2 (2 clusters x 3 nodes) @ v_new -> install_ravendb @ v_new ->
# form_clusters -> scenarios/EMR/RP1/rp1.yml.
#
# USAGE:
#   scenarios/EMR/RP1/run.sh <v_new_deb_path> [cluster_id_start] [docker_network_name]
#
# EXAMPLES:
#   # default single-lab run (cluster_id_start=1 -> hub=1, sink=2; network=hubsinknet)
#   scenarios/EMR/RP1/run.sh builds/raven-pr22875.deb
#
#   # parallel run on the same docker host -- clusters offset to 4 (hub=4, sink=5), isolated net
#   scenarios/EMR/RP1/run.sh builds/raven-pr22875.deb 4 rp1net
#
# For parallel concurrent runs (different scenarios on same machine), each run.sh invocation
# needs disjoint cluster_id_start ranges AND a unique docker_network_name so:
#   * container names don't collide on the docker daemon (globally unique by name)
#   * teardown nukes only that run's containers (scoped to docker_network_name)
###################################################################################################

set -euo pipefail

# Workaround for CPython 3.12.0-3.12.3 marshal bug -- compiling certain ansible
# collection modules raises "ValueError: unmarshallable object" when Python tries
# to write a .pyc cache.  Disabling bytecode-writing skips the buggy code path.
# Fixed upstream in Python 3.12.4; safe to keep set regardless.
export PYTHONDONTWRITEBYTECODE=1

V_NEW_BUILD="${1:-}"
CLUSTER_ID_START="${2:-1}"
DOCKER_NETWORK_NAME="${3:-hubsinknet}"
# Consume positionals; rest of "$@" is extra `-e foo=bar` overrides for the scenario.
shift 3 2>/dev/null || true
EXTRA_VARS=("$@")

if [ -z "$V_NEW_BUILD" ]; then
  echo "Usage: $0 <v_new_deb_path> [cluster_id_start] [docker_network_name]" >&2
  echo "       e.g.  $0 builds/raven-pr22875.deb" >&2
  echo "       e.g.  $0 builds/raven-pr22875.deb 4 rp1net    # parallel-friendly" >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

if [[ "$V_NEW_BUILD" != /* ]]; then
  V_NEW_BUILD="$REPO_ROOT/$V_NEW_BUILD"
fi
if [ ! -f "$V_NEW_BUILD" ]; then
  echo "ERROR: v_new .deb not found at $V_NEW_BUILD" >&2
  exit 3
fi

# prompt once for sudo password (skip if already exported)
if [ -z "${ANSIBLE_BECOME_PASS:-}" ]; then
  read -rsp "BECOME password (asked once, reused for the whole run): " ANSIBLE_BECOME_PASS
  echo
  export ANSIBLE_BECOME_PASS
fi

# RP-1 wants 2 clusters: hub at CLUSTER_ID_START, sink at CLUSTER_ID_START+1.
HUB_ID="$CLUSTER_ID_START"
SINK_ID=$((CLUSTER_ID_START + 1))

COMMON_OVERRIDES=(
    -e "cluster_id_start=$CLUSTER_ID_START"
    -e "hub_id=$HUB_ID"
    -e "sink_id=$SINK_ID"
    -e "docker_network_name=$DOCKER_NETWORK_NAME"
    -e "backups_volume_name=lab_backups_${DOCKER_NETWORK_NAME}"
)

echo "==> RP-1 run: hub=${HUB_ID}a/b/c  sink=${SINK_ID}a/b/c  docker_network_name=$DOCKER_NETWORK_NAME"
run() { echo; echo "==> $*"; "$@"; }

# 1. teardown any prior lab on this network (scoped to docker_network_name)
run ansible-playbook playbooks/teardown_containers.yml "${COMMON_OVERRIDES[@]}"

# 2. bring up T2 lab (2 clusters x 3 nodes) at v_new
run ansible-playbook playbooks/provision_nodes.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=2 -e nodes_per_cluster=3
run ansible-playbook playbooks/install_ravendb.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e custom_build="$V_NEW_BUILD" --skip-tags download
run ansible-playbook playbooks/form_clusters.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=2 -e nodes_per_cluster=3

# 3. run RP-1
# Forward any extra `-e` overrides the caller appended on the cmdline so smoke-mode
# sizing (e.g. -e bulk_users_sink1=200) actually reaches the scenario.
run ansible-playbook scenarios/EMR/RP1/rp1.yml \
    "${COMMON_OVERRIDES[@]}" \
    "${EXTRA_VARS[@]}"

echo
echo "==> RP-1 finished."
