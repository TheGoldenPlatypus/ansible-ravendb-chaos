#!/usr/bin/env bash
# scenarios/company-1/RV2/run.sh -- teardown + lab (3-node sharded src @ v_old + 1-node non-sharded tgt @ v_new) + rv2.yml.
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

SRC="$CID"; TGT=$((CID + 1))
C=( -e cluster_id_start="$CID" -e src_cluster_id="$SRC" -e tgt_cluster_id="$TGT"
    -e docker_network_name="$NET" -e backups_volume_name="lab_backups_$NET" )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RV-2  src=${SRC}a..c (sharded, 3 shards x 1 replica = 3 nodes; all-orchestrators) tgt=${TGT}a (non-sharded, 1 node)  net=$NET  v_old=$V_OLD"

# 3 sharded source nodes + 1 non-sharded target = 4 containers total.
# Two provision passes because nodes_per_cluster is a single uniform knob; each pass is
# scoped to its own cluster_id via cluster_id_start + clusters_count=1.  install_ravendb +
# form_clusters discover containers from the docker network so both runs see all 4.
step "teardown";        ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
step "provision src (3)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$SRC -e clusters_count=1 -e nodes_per_cluster=3
step "provision tgt (1)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$TGT -e clusters_count=1 -e nodes_per_cluster=1

# Default: role downloads v_old from S3.  Set V_OLD_DEB=<path> in env to use a cached .deb.
if [ -n "${V_OLD_DEB:-}" ]; then
  step "install v_old (cached: $V_OLD_DEB)"
  ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e custom_build="$V_OLD_DEB" --skip-tags download
else
  step "install v_old (S3 download)"
  ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e rdb_version="$V_OLD"
fi

step "form src (3)";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$SRC -e clusters_count=1 -e nodes_per_cluster=3
step "form tgt (1)";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$TGT -e clusters_count=1 -e nodes_per_cluster=1

# Upgrade target node to v_new so smuggler_import lands hashed-form revisions on it.
step "upgrade tgt";     ansible-playbook toolbox/service/upgrade_node.yml "${C[@]}" -e target="${TGT}a" -e custom_build="$V_NEW" --skip-tags download

step "rv2";             ansible-playbook scenarios/company-1/RV2/rv2.yml "${C[@]}" -e v_old="$V_OLD" -e v_new_build="$V_NEW" "${EXTRA[@]}"

step "RV-2 done"
