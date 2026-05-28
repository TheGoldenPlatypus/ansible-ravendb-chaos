#!/usr/bin/env bash
###################################################################################################
# scripts/reset_hub_sink.sh
#
# Rewinds the hub-sink chaos lab back to a clean baseline:
#   1. Heals every hub node (drops any leftover REJECT rules from a previous chaos run).
#   2. Hard-deletes Tenants on both clusters (also wipes the on-disk dir).
#   3. Recreates Tenants on both clusters.
#   4. Reruns define_hub + attach_sinks to rewire bidirectional pull-replication.
#
# Prompts for the sudo BECOME password ONCE, exports it as ANSIBLE_BECOME_PASS, and reuses it
# across every ansible-playbook call.  Each playbook still does `become: true` where needed --
# it just doesn't re-prompt.
#
# RUN:
#   ./scripts/reset_hub_sink.sh
###################################################################################################

set -euo pipefail

# cd to repo root regardless of where the script was invoked from
cd "$(dirname "$0")/.."

# Prompt once for sudo, stash for ansible's become.
read -rsp "BECOME password (asked once, reused for the whole script): " ANSIBLE_BECOME_PASS
echo
export ANSIBLE_BECOME_PASS

run() {
    echo
    echo "==> $*"
    "$@"
}

# 1. heal every hub node (drops leftover REJECT rules)
run ansible-playbook toolbox/network/heal_node.yml -e target=1a
run ansible-playbook toolbox/network/heal_node.yml -e target=1b
run ansible-playbook toolbox/network/heal_node.yml -e target=1c

# 2. delete Tenants on both clusters (hard delete -- wipes on-disk files)
run ansible-playbook toolbox/db/delete_database.yml -e cluster_leader=1a -e db_name=Tenants
run ansible-playbook toolbox/db/delete_database.yml -e cluster_leader=2a -e db_name=Tenants

# 3. recreate Tenants on both clusters
run ansible-playbook toolbox/db/create_database.yml -e cluster_leader=1a -e db_name=Tenants
run ansible-playbook toolbox/db/create_database.yml -e cluster_leader=2a -e db_name=Tenants

# 4. rewire bidirectional pull-replication
run ansible-playbook scenarios/hub-sink/tasks/define_hub.yml -e clusters_count=2
run ansible-playbook scenarios/hub-sink/tasks/attach_sinks.yml -e clusters_count=2

echo
echo "==> reset_hub_sink.sh DONE.  Hub-sink replication is rewired and Tenants is empty."
