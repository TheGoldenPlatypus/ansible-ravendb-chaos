#!/usr/bin/env bash
# scenarios/company-1/RPV2/run.sh -- teardown + 6-cluster lab @ v_new + rpv2.yml.
# Usage: run.sh <v_new_deb> [cluster_id_start=1] [docker_network=hubsinknet] [-e foo=bar ...]
#
# Cluster layout (6 clusters, Karmel-faithful):
#   <CID>     hub          (3 nodes, RF=3)
#   <CID+1>   sink-1       (3 nodes, RF=3 -- needed for round-2 sink-leader failover)
#   <CID+2>   r1-snap-tgt  (1 node)
#   <CID+3>   r1-smug-tgt  (1 node)
#   <CID+4>   r2-snap-tgt  (1 node)
#   <CID+5>   r2-smug-tgt  (1 node)
#
# provision_nodes.yml / form_clusters.yml only support a uniform nodes_per_cluster,
# so we call them once per cluster -- the same pattern RV-3's run.sh uses for its
# mixed 9/1/9/1/1 layout.
#
# Phase-(e) "fresh sink" lives as a fresh database (db1_resink) on the smuggler-restore
# cluster -- saves a 7th cluster without breaking restore-target isolation.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V_NEW="${POS[0]:-}"; CID="${POS[1]:-1}"; NET="${POS[2]:-hubsinknet}"

[ -n "$V_NEW" ] || { echo "Usage: $0 <v_new_deb> [cid=1] [net=hubsinknet] [-e foo=bar ...]"; exit 2; }

cd "$(dirname "$0")/../../.."
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }

[ -z "${ANSIBLE_BECOME_PASS:-}" ] && { read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS; }

HUB="$CID"
S1=$((CID + 1))
R1_SNAP=$((CID + 2))
R1_SMUG=$((CID + 3))
R2_SNAP=$((CID + 4))
R2_SMUG=$((CID + 5))

# Shared overrides for every play -- IDs + docker net + backups volume.
C=( -e cluster_id_start="$CID" \
    -e hub_id="$HUB" -e sink1_id="$S1" \
    -e r1_snap_id="$R1_SNAP" -e r1_smug_id="$R1_SMUG" \
    -e r2_snap_id="$R2_SNAP" -e r2_smug_id="$R2_SMUG" \
    -e docker_network_name="$NET" -e backups_volume_name="lab_backups_$NET" )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RPV-2  hub=${HUB}a/b/c  sink1=${S1}a/b/c  r1-snap=${R1_SNAP}a  r1-smug=${R1_SMUG}a  r2-snap=${R2_SNAP}a  r2-smug=${R2_SMUG}a  net=$NET"

step "teardown";      ansible-playbook playbooks/teardown_containers.yml "${C[@]}"

# Six provision passes -- one per cluster, each with its own nodes_per_cluster.
step "provision hub (3)";    ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$HUB     -e clusters_count=1 -e nodes_per_cluster=3
step "provision sink1 (3)";  ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$S1      -e clusters_count=1 -e nodes_per_cluster=3
step "provision r1-snap (1)";ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$R1_SNAP -e clusters_count=1 -e nodes_per_cluster=1
step "provision r1-smug (1)";ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$R1_SMUG -e clusters_count=1 -e nodes_per_cluster=1
step "provision r2-snap (1)";ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$R2_SNAP -e clusters_count=1 -e nodes_per_cluster=1
step "provision r2-smug (1)";ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$R2_SMUG -e clusters_count=1 -e nodes_per_cluster=1

step "install v_new";   ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e custom_build="$V_NEW" --skip-tags download

# Six form passes -- mirror provision.
step "form hub";        ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$HUB     -e clusters_count=1 -e nodes_per_cluster=3
step "form sink1";      ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$S1      -e clusters_count=1 -e nodes_per_cluster=3
step "form r1-snap";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$R1_SNAP -e clusters_count=1 -e nodes_per_cluster=1
step "form r1-smug";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$R1_SMUG -e clusters_count=1 -e nodes_per_cluster=1
step "form r2-snap";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$R2_SNAP -e clusters_count=1 -e nodes_per_cluster=1
step "form r2-smug";    ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$R2_SMUG -e clusters_count=1 -e nodes_per_cluster=1

step "rpv2";           ansible-playbook scenarios/company-1/RPV2/rpv2.yml "${C[@]}" -e v_new_build="$V_NEW" "${EXTRA[@]}"

step "RPV-2 done"
