#!/usr/bin/env bash
###################################################################################################
# scenarios/EMR/RV1/run.sh -- run the full RV-1 test end-to-end.
#
# Teardown -> provision (v_old) -> install_ravendb -> form_clusters -> scenarios/EMR/RV1/rv1.yml.
#
# USAGE:
#   scenarios/EMR/RV1/run.sh <v_old_version> <v_new_deb_path>
#
# EXAMPLE:
#   scenarios/EMR/RV1/run.sh 6.2.15 builds/raven-pr22875.deb
#
# Build the v_new .deb first with:
#   scripts/build_ravendb_pr.sh <pr-number>
###################################################################################################

set -euo pipefail

V_OLD="${1:-}"
V_NEW_BUILD="${2:-}"

if [ -z "$V_OLD" ] || [ -z "$V_NEW_BUILD" ]; then
  echo "Usage: $0 <v_old_version> <v_new_deb_path>" >&2
  echo "       e.g.  $0 6.2.15 builds/raven-pr22875.deb" >&2
  exit 2
fi

# this script lives at <repo>/scenarios/EMR/RV1/run.sh -- climb to repo root
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

# resolve v_new .deb to absolute path
if [[ "$V_NEW_BUILD" != /* ]]; then
  V_NEW_BUILD="$REPO_ROOT/$V_NEW_BUILD"
fi
if [ ! -f "$V_NEW_BUILD" ]; then
  echo "ERROR: v_new .deb not found at $V_NEW_BUILD" >&2
  echo "       Build it with: scripts/build_ravendb_pr.sh <pr-number>" >&2
  exit 3
fi

# prompt once for sudo password (skip if already exported)
if [ -z "${ANSIBLE_BECOME_PASS:-}" ]; then
  read -rsp "BECOME password (asked once, reused for the whole run): " ANSIBLE_BECOME_PASS
  echo
  export ANSIBLE_BECOME_PASS
fi

run() {
  echo
  echo "==> $*"
  "$@"
}

# 1. teardown any prior lab (also removes lab_backups volume + captures/)
run ansible-playbook playbooks/teardown_containers.yml

# 2. bring up a clean 1-cluster x 3-node lab at v_old
run ansible-playbook playbooks/provision_nodes.yml \
    -e clusters_count=1 -e nodes_per_cluster=3
run ansible-playbook playbooks/install_ravendb.yml -e rdb_version="$V_OLD"
run ansible-playbook playbooks/form_clusters.yml \
    -e clusters_count=1 -e nodes_per_cluster=3

# 3. run RV-1 with the PR build as v_new
run ansible-playbook scenarios/EMR/RV1/rv1.yml \
    -e v_old="$V_OLD" \
    -e v_new_build="$V_NEW_BUILD"

echo
echo "==> RV-1 finished."
