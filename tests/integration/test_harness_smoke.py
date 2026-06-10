"""
Smoke test for the integration harness itself: brings up a single embedded
node, creates a db, runs k_doc_count against it.  If this passes, the harness
is wired correctly and multi-node tests can use the same plumbing.
"""

import ravendb_diagnostic as diag


def _params(target, db="db1"):
    return {
        "target": target,
        "db_name": db,
        "ravendb_domain": "ignored-when-override-is-set",
        "client_cert": "",
        "ca_cert": "",
    }


def test_single_node_db_is_reachable_via_diagnostic(ravendb_cluster):
    """Bring up 1 node, create db1, call k_doc_count. Should return a string
    naming the target and db with a count of 0 (empty db)."""
    info = ravendb_cluster(n_nodes=1, db="db1")
    target = info["nodes"][0]

    msg = diag.k_doc_count(_params(target))

    assert "%s/db1" % target in msg
    assert "0 docs" in msg
