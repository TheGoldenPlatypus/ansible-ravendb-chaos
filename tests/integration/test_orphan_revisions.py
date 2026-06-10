"""
Tests for k_orphan_revisions.

What the kind does:  POSTs /admin/revisions/orphaned/adopt on every node, polls
                     /operations/state until terminal, FAILs if any node reports
                     a non-zero AdoptedCount.  Marks INCONCLUSIVE when RavenDB
                     op-id recycling returns a non-adopt $type.
Returns:             list[str]
Raises:              RuntimeError if any node is unreachable, if trigger returns
                     non-2xx, or if operations don't finish within budget_secs.
                     DiagnosticViolation in assert_mode when orphans were
                     present.
"""

import json

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def test_no_orphans_passes(ravendb_cluster):
    """Empty DB -> no orphans -> PASS, AdoptedCount=0 everywhere."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_orphan_revisions(params(nodes=[node], assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: 'PASS  no orphan revisions on any node'")
    print_lines("actual", lines)
    print()
    assert "PASS  no orphan revisions on any node" in text


def _fake_request_adopted_count(adopted_count):
    """Adopt POST returns op-id 1; /operations/state returns Completed with
    AdoptOrphanedRevisionsResult.AdoptedCount = <adopted_count>."""
    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        if "/admin/revisions/orphaned/adopt" in path:
            return 200, json.dumps({"OperationId": 1}).encode()
        if "/operations/state" in path:
            return 200, json.dumps({
                "Status": "Completed",
                "Result": {
                    "$type": "AdoptOrphanedRevisionsResult, Raven.Server",
                    "AdoptedCount": adopted_count,
                },
            }).encode()
        return 404, b""
    return fake_request


def test_orphans_present_fails(ravendb_cluster, monkeypatch):
    """Engineering real orphan revisions is heavy (delete+revisions keeps a
    delete-marker, not orphans).  Monkeypatch /operations/state to report
    AdoptedCount=5 -> info-mode FAIL line."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    monkeypatch.setattr(diag, "request", _fake_request_adopted_count(5))

    lines = diag.k_orphan_revisions(params(nodes=[node]))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  orphans were present on at least one node'")
    print_lines("actual", lines)
    print()
    assert "FAIL  orphans were present on at least one node" in text
    assert "5" in text   # adopted count surfaced in per-node table


def test_assert_mode_raises_on_orphans(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    monkeypatch.setattr(diag, "request", _fake_request_adopted_count(3))

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  orphans were present'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_orphan_revisions(params(nodes=[node], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  orphans were present" in exc.value.lines[-1]


def test_unreachable_node_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_orphan_revisions(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)


def test_inconclusive_path_via_op_id_recycling(ravendb_cluster, monkeypatch):
    """RavenDB recycles op-ids on fast operations.  If /operations/state
    returns a Completed result whose $type isn't AdoptOrphanedRevisionsResult,
    the kind marks the node INCONCLUSIVE and falls back to 'OK (partial)'
    instead of FAIL."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        if "/admin/revisions/orphaned/adopt" in path:
            return 200, json.dumps({"OperationId": 42}).encode()
        if "/operations/state" in path:
            return 200, json.dumps({
                "Status": "Completed",
                "Result": {"$type": "SomeOtherOperationResult, Raven.Server"},
            }).encode()
        return 404, b""
    monkeypatch.setattr(diag, "request", fake_request)

    lines = diag.k_orphan_revisions(params(nodes=[node]))
    text = "\n".join(lines)

    print(f"\n    expected: 'INCONCLUSIVE on' + 'OK (partial)', no FAIL")
    print_lines("actual", lines)
    print()
    assert "INCONCLUSIVE on" in text
    assert "OK (partial)" in text
    assert "FAIL" not in text


def test_budget_exhausted_raises(ravendb_cluster, monkeypatch):
    """Operation never reaches a terminal Status -> RuntimeError after budget."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        if "/admin/revisions/orphaned/adopt" in path:
            return 200, json.dumps({"OperationId": 7}).encode()
        if "/operations/state" in path:
            return 200, json.dumps({"Status": "Running"}).encode()
        return 404, b""
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: raises RuntimeError mentioning 'never finished'")
    with pytest.raises(RuntimeError, match="never finished") as exc:
        diag.k_orphan_revisions(params(nodes=[node], budget_secs=1))
    print(f"    actual:   {exc.value!s}\n")
