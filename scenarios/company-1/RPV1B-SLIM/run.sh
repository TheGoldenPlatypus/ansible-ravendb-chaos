#!/usr/bin/env bash
# scenarios/company-1/RPV1B-SLIM/run.sh -- teardown + T2 lab @ v_new + rpv1b_slim.yml.
# Usage: run.sh <v_new_deb> [cluster_id_start=1] [docker_network=hubsinknet] [-e foo=bar ...]
#
# RPV-1B-SLIM is the steady-state variant of RPV-1: T2 topology (1 hub + 1 sink, all v_new
# from the start, NO rolling upgrade), W-1C cluster-wide-tx workloads ON by default,
# feature flags enabled upfront before seed.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # CPython 3.12.0-3.12.3 marshal bug

# Pull positionals up to the first -flag; everything from there on is forwarded -e overrides.
POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V_NEW="${POS[0]:-}"; CID="${POS[1]:-1}"; NET="${POS[2]:-hubsinknet}"

[ -n "$V_NEW" ] || { echo "Usage: $0 <v_new_deb> [cid=1] [net=hubsinknet] [-e foo=bar ...]"; exit 2; }

cd "$(dirname "$0")/../../.."
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }

[ -z "${ANSIBLE_BECOME_PASS:-}" ] && { read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS; }

HUB="$CID"; SINK=$((CID + 1))

# Per-iter controller-side host port for ConsistencyCheck's state-store container.
# Default 8084 collides on parallel iters; derive from CID so each iter binds a
# unique slot (gap of 10 leaves headroom):
#   CID 1   (single)      -> state=8084
#   CID 30  (parallel #1) -> state=8084
#   CID 130 (parallel #2) -> state=8094
#   CID 230 (parallel #3) -> state=8104
PORT_OFFSET=$(( (CID / 100) * 10 ))
CC_STATE_PORT=$(( 8084 + PORT_OFFSET ))

C=( -e cluster_id_start="$CID" -e hub_id="$HUB" -e sink_id="$SINK"
    -e docker_network_name="$NET" -e backups_volume_name="lab_backups_$NET"
    -e cc_state_port="$CC_STATE_PORT" )
# Overnight runner sets DOCKER_NETWORK_SUBNET to pin container IPs so post-mortem
# `docker start` returns each container to its original IP.  Single-scenario runs
# leave it unset and fall back to Docker auto-assign.
[ -n "${DOCKER_NETWORK_SUBNET:-}" ] && C+=( -e docker_network_subnet="$DOCKER_NETWORK_SUBNET" )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RPV-1B-SLIM  hub=${HUB}a/b/c  sink=${SINK}a/b/c  net=$NET  (all v_new, no upgrade)"

step "teardown";    ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
step "provision";   ansible-playbook playbooks/provision_nodes.yml     "${C[@]}" -e clusters_count=2 -e nodes_per_cluster=3
step "install";     ansible-playbook playbooks/install_ravendb.yml     "${C[@]}" -e custom_build="$V_NEW" --skip-tags download
step "form";        ansible-playbook playbooks/form_clusters.yml       "${C[@]}" -e clusters_count=2 -e nodes_per_cluster=3
step "rpv1b_slim";  ansible-playbook scenarios/company-1/RPV1B-SLIM/rpv1b_slim.yml "${C[@]}" "${EXTRA[@]}"

step "RPV-1B-SLIM done"
