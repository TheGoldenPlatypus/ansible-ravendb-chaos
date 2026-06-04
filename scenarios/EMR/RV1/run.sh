#!/usr/bin/env bash
###################################################################################################
# scenarios/EMR/RV1/run.sh -- run the full RV-1 test end-to-end.
#
# Teardown -> provision 1 cluster x 3 nodes @ v_old -> install_ravendb @ v_old ->
# form_clusters -> scenarios/EMR/RV1/rv1.yml.
#
# USAGE:
#   scenarios/EMR/RV1/run.sh <v_old_version> <v_new_deb_path> [cluster_id_start] [docker_network_name]
#
# EXAMPLES:
#   # default single-lab run (cluster_id_start=1, network=hubsinknet)
#   scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb
#
#   # parallel run on the same docker host -- clusters offset to 4, isolated network
#   scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb 4 rv1net
#
# For parallel concurrent runs (different scenarios on same machine), each run.sh invocation
# needs disjoint cluster_id_start ranges AND a unique docker_network_name so:
#   * container names don't collide on the docker daemon (globally unique by name)
#   * teardown nukes only that run's containers (scoped to docker_network_name)
#
# Build the v_new .deb first with:
#   scripts/build_ravendb_pr.sh <pr-number>
###################################################################################################

set -euo pipefail

# Workaround for CPython 3.12.0-3.12.3 marshal bug -- compiling certain ansible
# collection modules raises "ValueError: unmarshallable object" when Python tries
# to write a .pyc cache.  Disabling bytecode-writing skips the buggy code path.
# Fixed upstream in Python 3.12.4; safe to keep set regardless.
export PYTHONDONTWRITEBYTECODE=1

V_OLD="${1:-}"
V_NEW_BUILD="${2:-}"
CLUSTER_ID_START="${3:-1}"
DOCKER_NETWORK_NAME="${4:-hubsinknet}"
# Consume the 4 positional args; whatever's left in "$@" is extra `-e foo=bar` overrides
# the caller wants to pass through to the scenario playbook (e.g. smoke-mode sizing).
shift 4 2>/dev/null || true
EXTRA_VARS=("$@")

if [ -z "$V_OLD" ] || [ -z "$V_NEW_BUILD" ]; then
  echo "Usage: $0 <v_old_version> <v_new_deb_path> [cluster_id_start] [docker_network_name]" >&2
  echo "       e.g.  $0 6.2.15 builds/raven-pr22875.deb" >&2
  echo "       e.g.  $0 6.2.15 builds/raven-pr22875.deb 4 rv1net    # parallel-friendly" >&2
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

# Common overrides plumbed through every playbook in this run.
COMMON_OVERRIDES=(
    -e "cluster_id_start=$CLUSTER_ID_START"
    -e "cluster_id=$CLUSTER_ID_START"
    -e "docker_network_name=$DOCKER_NETWORK_NAME"
    -e "backups_volume_name=lab_backups_${DOCKER_NETWORK_NAME}"
)

echo "==> RV-1 run: cluster_id_start=$CLUSTER_ID_START  docker_network_name=$DOCKER_NETWORK_NAME"
run() { echo; echo "==> $*"; "$@"; }

# 1. teardown any prior lab on this network (scoped to docker_network_name)
run ansible-playbook playbooks/teardown_containers.yml "${COMMON_OVERRIDES[@]}"

# 2. bring up a clean single-cluster x 3-node lab at v_old
run ansible-playbook playbooks/provision_nodes.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=1 -e nodes_per_cluster=3
run ansible-playbook playbooks/install_ravendb.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e rdb_version="$V_OLD"
run ansible-playbook playbooks/form_clusters.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=1 -e nodes_per_cluster=3

# 3. run RV-1.  Forward any extra `-e` overrides the caller appended on the cmdline so
#    smoke-mode sizing (e.g. -e phase1_seed_count=500) actually reaches the scenario.
run ansible-playbook scenarios/EMR/RV1/rv1.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e v_old="$V_OLD" \
    -e v_new_build="$V_NEW_BUILD" \
    "${EXTRA_VARS[@]}"

echo
echo "==> RV-1 finished."
