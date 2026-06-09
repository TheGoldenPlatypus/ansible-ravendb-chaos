#!/usr/bin/env bash
# scenarios/company-1/RV3/run.sh -- teardown + T4-extended lab (5 clusters, 21 nodes) + rv3.yml.
# Usage: run.sh <v_new_deb> [cluster_id_start=1] [docker_network=hubsinknet] [-e foo=bar ...]
# All 5 clusters run at v_new (the build under test); RV-3 has no rolling upgrade.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1  # CPython 3.12.0-3.12.3 marshal bug

POS=()
while [ $# -gt 0 ]; do case "$1" in -*) break;; *) POS+=("$1"); shift;; esac; done
EXTRA=("$@")

V_NEW="${POS[0]:-}"; CID="${POS[1]:-1}"; NET="${POS[2]:-hubsinknet}"

[ -n "$V_NEW" ] || { echo "Usage: $0 <v_new_deb> [cid=1] [net=hubsinknet] [-e foo=bar ...]"; exit 2; }

cd "$(dirname "$0")/../../.."
[[ "$V_NEW" = /* ]] || V_NEW="$PWD/$V_NEW"
[ -f "$V_NEW" ] || { echo "ERROR: $V_NEW not found"; exit 3; }

[ -z "${ANSIBLE_BECOME_PASS:-}" ] && { read -rsp "BECOME password: " ANSIBLE_BECOME_PASS; echo; export ANSIBLE_BECOME_PASS; }

# 5 cluster ids starting at CID.
C1=$CID                  # sharded source (9 nodes)
C2=$((CID + 1))          # non-sharded source (1)
C3=$((CID + 2))          # sharded import target (9)
C4=$((CID + 3))          # ETL target (1)
C5=$((CID + 4))          # backup comparison target (1)

C=( -e cluster_id_start=$CID
    -e src_sharded_cluster_id=$C1
    -e nonshd_src_cluster_id=$C2
    -e sharded_tgt_cluster_id=$C3
    -e etl_tgt_cluster_id=$C4
    -e bkp_cmp_cluster_id=$C5
    -e docker_network_name=$NET
    -e backups_volume_name=lab_backups_$NET )

step() { printf '\n\033[1;36m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }

echo "RV-3  shd-src=${C1}a..i  non-src=${C2}a  shd-tgt=${C3}a..i  etl-tgt=${C4}a  bkp-cmp=${C5}a  net=$NET"

# Five provision passes -- one per cluster, each with its own nodes_per_cluster size.
# install_ravendb + form_clusters discover containers from the docker network, so all 21
# end up reachable.
step "teardown";              ansible-playbook playbooks/teardown_containers.yml "${C[@]}"
step "provision shd-src (9)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$C1 -e clusters_count=1 -e nodes_per_cluster=9
step "provision non-src (1)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$C2 -e clusters_count=1 -e nodes_per_cluster=1
step "provision shd-tgt (9)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$C3 -e clusters_count=1 -e nodes_per_cluster=9
step "provision etl-tgt (1)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$C4 -e clusters_count=1 -e nodes_per_cluster=1
step "provision bkp-cmp (1)"; ansible-playbook playbooks/provision_nodes.yml "${C[@]}" -e cluster_id_start=$C5 -e clusters_count=1 -e nodes_per_cluster=1

step "install v_new"; ansible-playbook playbooks/install_ravendb.yml "${C[@]}" -e custom_build="$V_NEW" --skip-tags download

step "form shd-src (9)"; ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$C1 -e clusters_count=1 -e nodes_per_cluster=9
step "form non-src (1)"; ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$C2 -e clusters_count=1 -e nodes_per_cluster=1
step "form shd-tgt (9)"; ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$C3 -e clusters_count=1 -e nodes_per_cluster=9
step "form etl-tgt (1)"; ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$C4 -e clusters_count=1 -e nodes_per_cluster=1
step "form bkp-cmp (1)"; ansible-playbook playbooks/form_clusters.yml "${C[@]}" -e cluster_id_start=$C5 -e clusters_count=1 -e nodes_per_cluster=1

step "rv3"; ansible-playbook scenarios/company-1/RV3/rv3.yml "${C[@]}" -e v_new_build="$V_NEW" "${EXTRA[@]}"

step "RV-3 done"
