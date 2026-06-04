#!/usr/bin/env bash
###################################################################################################
# scenarios/EMR/RPV1/run.sh -- run the full RPV-1 test end-to-end.
#
# Teardown -> provision T3 (3 clusters x 3 nodes) @ v_old -> install_ravendb @ v_old ->
# form_clusters -> scenarios/EMR/RPV1/rpv1.yml.
#
# USAGE:
#   scenarios/EMR/RPV1/run.sh <v_old_version> <v_new_deb_path> [cluster_id_start] [docker_network_name]
#
# EXAMPLES:
#   # default single-lab run (hub=1, sink1=2, sink2=3; network=hubsinknet)
#   scenarios/EMR/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb
#
#   # parallel run on the same docker host -- clusters offset to 4 (hub=4, sink1=5, sink2=6)
#   scenarios/EMR/RPV1/run.sh 6.2.15 builds/raven-pr22875.deb 4 rpv1net
#
# For parallel concurrent runs (different scenarios on same machine), each run.sh invocation
# needs disjoint cluster_id_start ranges AND a unique docker_network_name.
###################################################################################################

set -euo pipefail

V_OLD="${1:-}"
V_NEW_BUILD="${2:-}"
CLUSTER_ID_START="${3:-1}"
DOCKER_NETWORK_NAME="${4:-hubsinknet}"

if [ -z "$V_OLD" ] || [ -z "$V_NEW_BUILD" ]; then
  echo "Usage: $0 <v_old_version> <v_new_deb_path> [cluster_id_start] [docker_network_name]" >&2
  echo "       e.g.  $0 6.2.15 builds/raven-pr22875.deb" >&2
  echo "       e.g.  $0 6.2.15 builds/raven-pr22875.deb 4 rpv1net    # parallel-friendly" >&2
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

# RPV-1 wants 3 clusters: hub at CLUSTER_ID_START, sink1 +1, sink2 +2.
HUB_ID="$CLUSTER_ID_START"
SINK1_ID=$((CLUSTER_ID_START + 1))
SINK2_ID=$((CLUSTER_ID_START + 2))

COMMON_OVERRIDES=(
    -e "cluster_id_start=$CLUSTER_ID_START"
    -e "hub_id=$HUB_ID"
    -e "sink1_id=$SINK1_ID"
    -e "sink2_id=$SINK2_ID"
    -e "docker_network_name=$DOCKER_NETWORK_NAME"
)

echo "==> RPV-1 run: hub=${HUB_ID}a/b/c  sink1=${SINK1_ID}a/b/c  sink2=${SINK2_ID}a/b/c  docker_network_name=$DOCKER_NETWORK_NAME"
run() { echo; echo "==> $*"; "$@"; }

# 1. teardown any prior lab on this network
run ansible-playbook playbooks/teardown_containers.yml "${COMMON_OVERRIDES[@]}"

# 2. bring up T3 lab (3 clusters x 3 nodes) at v_old
run ansible-playbook playbooks/provision_nodes.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=3 -e nodes_per_cluster=3
run ansible-playbook playbooks/install_ravendb.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e rdb_version="$V_OLD"
run ansible-playbook playbooks/form_clusters.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e clusters_count=3 -e nodes_per_cluster=3

# 3. run RPV-1
run ansible-playbook scenarios/EMR/RPV1/rpv1.yml \
    "${COMMON_OVERRIDES[@]}" \
    -e v_old="$V_OLD" \
    -e v_new_build="$V_NEW_BUILD"

echo
echo "==> RPV-1 finished."
