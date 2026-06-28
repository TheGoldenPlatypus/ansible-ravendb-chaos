#!/usr/bin/env bash
# scenarios/company-1/RPV1-NOREV/run.sh -- teardown + T3 lab @ v_old + rpv1_norev.yml.
# Variant of RPV-1 with per-collection revisions configuration removed.  Otherwise
# identical: T3 cross-cluster rolling upgrade, filter-aware pull replication,
# W-1 + W-2 hub workloads, W-1 on each sink.  Used to isolate whether the
# replication anomalies in RPV-1 are revision-config related or independent.
# Usage: run.sh <v_old> <v_new_deb> [cluster_id_start=1] [docker_network=hubsinknet] [-e foo=bar ...]
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # CPython 3.12.0-3.12.3 marshal bug

# Pull positionals up to the first -flag; everything from there on is forwarded -e overrides.
POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V_OLD="${POS[0]:-}"; V_NEW="${POS[1]:-}"; CID="${POS[2]:-1}"; NET="${POS[3]:-hubsinknet}"

[ -n "$V_OLD" ] && [ -n "$V_NEW" ] || { echo "Usage: $0 <v_old> <v_new_deb> [cid=1] [net=hubsinknet] [-e foo=bar ...]"; exit 2; }

cd "$(dirname "$0")/../../.."
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }

[ -z "${ANSIBLE_BECOME_PASS:-}" ] && { read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS; }

HUB="$CID"; S1=$((CID + 1)); S2=$((CID + 2))

# Per-iter controller-side host port for ConsistencyCheck's state-store container.
# Default 8084 collides on parallel iters; derive from CID so each iter binds a
# unique slot (gap of 10 leaves headroom):
#   CID 1   (single)      -> state=8084
#   CID 30  (parallel #1) -> state=8084
#   CID 130 (parallel #2) -> state=8094
#   CID 230 (parallel #3) -> state=8104
# No --health-bind on the one-shot profile, so no health-port var needed.
PORT_OFFSET=$(( (CID / 100) * 10 ))
CC_STATE_PORT=$(( 8084 + PORT_OFFSET ))

C=( -e cluster_id_start="$CID" -e hub_id="$HUB" -e sink1_id="$S1" -e sink2_id="$S2"
    -e docker_network_name="$NET" -e backups_volume_name="lab_backups_$NET"
    -e cc_state_port="$CC_STATE_PORT" )
# Overnight runner sets DOCKER_NETWORK_SUBNET to pin container IPs so post-mortem
# `docker start` returns each container to its original IP.  Single-scenario runs
# leave it unset and fall back to Docker auto-assign.
[ -n "${DOCKER_NETWORK_SUBNET:-}" ] && C+=( -e docker_network_subnet="$DOCKER_NETWORK_SUBNET" )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RPV-1-NOREV  hub=${HUB}a/b/c  sink1=${S1}a/b/c  sink2=${S2}a/b/c  net=$NET  v_old=$V_OLD  (no revisions config)"

step "teardown";    ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
step "provision";   ansible-playbook playbooks/provision_nodes.yml     "${C[@]}" -e clusters_count=3 -e nodes_per_cluster=3
# Default: role downloads v_old from S3.  Set V_OLD_DEB=<path> in env to use a cached .deb.
if [ -n "${V_OLD_DEB:-}" ]; then
  step "install v_old (cached: $V_OLD_DEB)"
  ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e custom_build="$V_OLD_DEB" --skip-tags download
else
  step "install v_old (S3 download)"
  ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e rdb_version="$V_OLD"
fi
step "form";        ansible-playbook playbooks/form_clusters.yml       "${C[@]}" -e clusters_count=3 -e nodes_per_cluster=3
step "rpv1_norev"; ansible-playbook scenarios/company-1/RPV1-NOREV/rpv1_norev.yml "${C[@]}" -e v_old="$V_OLD" -e v_new_build="$V_NEW" "${EXTRA[@]}"

step "RPV-1-NOREV done"
