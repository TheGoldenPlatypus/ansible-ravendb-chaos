"""
Tests for ravendb_wait hardening: no vacuous PASSes, no silent-skip on
unreachable nodes, no orphan-shard exclusion.

What the module does:  k_etag_parity / k_docs_drain / k_quiescence /
                       k_stats_field_parity / k_conflicts_resolved /
                       k_marker_propagation / k_doc_count_match poll the
                       cluster until convergence.  classify_nodes + the
                       snapshot helper are load-bearing under every one of
                       these.

Hardening focus:       Karmel told us 'scenarios must fail against v_new
                       because v_new is incomplete'.  If a wait silently
                       PASSes when the cluster is partitioned, the v_new
                       failure mode goes undetected.  These tests pin the
                       failure paths:
                         * classify_nodes raises on connection-error nodes
                           (no more conflating with 'wrong cluster').
                         * snapshot_stats_field raises on ANY node that
                           failed (no more silent drop -> all([])==True).
                         * predicates have defense-in-depth empty guards.
                         * k_conflicts_resolved raises on connection error
                           instead of looping to timeout with a misleading
                           'never converged'.
                         * k_marker_propagation raises on unreachable target
                           instead of looping to timeout.
                         * k_doc_count_match raises on orphan shard (empty
                           Members) instead of silently excluding it.

All tests monkeypatch ravendb_wait.request and ravendb_wait.request_per_node.
No real RavenDB needed.
"""

import json
from collections import defaultdict

import pytest

import ravendb_wait as waitmod


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["timeout"] = 5
    p["poll_interval"] = 0       # spin fast in tests
    p.update(kwargs)
    return p


def _stats_body(field_values):
    """Build a /stats response body containing each (field, value) pair."""
    return json.dumps(field_values).encode()


# ============================================================================
# classify_nodes: connection errors must raise, not silently skip
# ============================================================================

def test_classify_nodes_raises_when_a_node_is_unreachable(monkeypatch):
    """One node returns 200 (healthy), the other returns status=None
    (request_per_node caught a network exception).  Old code would silently
    add the dead node to `skipped` and proceed with degraded coverage; new
    code raises loud."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"CountOfDocuments": 5})),
            ("1b", None, "connection refused"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning '1b' and 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        waitmod.classify_nodes(params(), ["1a", "1b"])
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_classify_nodes_tolerates_genuine_wrong_cluster_404(monkeypatch):
    """A node responding 404 means 'I'm in the cluster but don't host this
    db' -- that's a legitimate skip path, NOT an unreachable.  Must succeed
    with the 404 node in the skipped list."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({})),
            ("1b", 404, b"db not found here"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    host_map, skipped = waitmod.classify_nodes(params(), ["1a", "1b"])
    print(f"\n    expected: host_map={{'1a': None}}  skipped=['1b']")
    print(f"    actual:   host_map={host_map}  skipped={skipped}\n")
    assert host_map == {"1a": None}
    assert skipped == ["1b"]


# ============================================================================
# snapshot_stats_field: any node failure must raise
# ============================================================================

def test_snapshot_stats_field_raises_when_a_node_returns_non_200(monkeypatch):
    """The dict path -- some node returns HTTP 500.  Old code silently
    dropped it from `snap`, so `all(prev.get(n) == current.get(n) for n in
    has_db)` could vacuously pass.  New code raises loud."""
    state = {"calls": 0}
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        state["calls"] += 1
        if target == "1a":
            return 200, _stats_body({"LastDatabaseEtag": 42})
        return 500, b'{"error":"fake"}'
    monkeypatch.setattr(waitmod, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500' and '1/2 node(s)'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        waitmod.snapshot_stats_field(params(),
                                      {"1a": None, "1b": None},
                                      "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_snapshot_stats_field_raises_when_connection_error_on_dict_path(monkeypatch):
    """Dict path with a node that raises a network exception.  Must raise."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        if target == "1a":
            return 200, _stats_body({"LastDatabaseEtag": 42})
        raise ConnectionRefusedError("network unreachable")
    monkeypatch.setattr(waitmod, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'connection error'")
    with pytest.raises(RuntimeError, match="connection error") as exc:
        waitmod.snapshot_stats_field(params(),
                                      {"1a": None, "1b": None},
                                      "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_snapshot_stats_field_raises_when_field_missing_in_response(monkeypatch):
    """Response is 200 but the field is absent.  Old code would have stored
    None via .get(field) and treated it as 'data'; new code raises."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        return 200, _stats_body({"SomeOtherField": 1})
    monkeypatch.setattr(waitmod, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'no .LastDatabaseEtag. field'")
    with pytest.raises(RuntimeError, match="no 'LastDatabaseEtag' field") as exc:
        waitmod.snapshot_stats_field(params(),
                                      {"1a": None},
                                      "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")


def test_snapshot_stats_field_raises_on_legacy_list_path_with_dead_node(monkeypatch):
    """Plain-list path (legacy non-sharded).  One node status=None -> raise."""
    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [
            ("1a", 200, _stats_body({"LastDatabaseEtag": 42})),
            ("1b", None, "ConnectionRefusedError('1b')"),
        ]
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'connection error' and '1b'")
    with pytest.raises(RuntimeError, match="connection error") as exc:
        waitmod.snapshot_stats_field(params(), ["1a", "1b"], "LastDatabaseEtag")
    print(f"    actual:   {exc.value!s}\n")
    assert "1b" in str(exc.value)


def test_snapshot_stats_field_happy_path(monkeypatch):
    """Every node responds 200 with the field -> returns the snap dict."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        return 200, _stats_body({"LastDatabaseEtag": {"1a": 1, "1b": 2}[target]})
    monkeypatch.setattr(waitmod, "request", fake)

    snap = waitmod.snapshot_stats_field(params(),
                                         {"1a": None, "1b": None},
                                         "LastDatabaseEtag")
    print(f"\n    expected: {{'1a': 1, '1b': 2}}")
    print(f"    actual:   {snap}\n")
    assert snap == {"1a": 1, "1b": 2}


# ============================================================================
# k_quiescence: all-None-CV must NOT be treated as convergence
# ============================================================================

def test_k_quiescence_raises_when_every_node_returns_missing_cv(monkeypatch):
    """All nodes respond 200 with DatabaseChangeVector=null.  Old code:
    unique={None}, len==1 -> 'QUIESCENT'.  Real meaning: every node returned
    a missing/empty CV (e.g. brand-new DB never written to).  New code:
    raise loud."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: None for n in host_map}      # everyone returns None

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    print(f"\n    expected: RuntimeError mentioning 'missing/empty'")
    with pytest.raises(RuntimeError, match="missing/empty") as exc:
        waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")


def test_k_quiescence_raises_when_every_node_returns_empty_string_cv(monkeypatch):
    """Same false-convergence shape but with empty strings instead of None."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: "" for n in host_map}

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    print(f"\n    expected: RuntimeError mentioning 'missing/empty'")
    with pytest.raises(RuntimeError, match="missing/empty") as exc:
        waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")


def test_k_quiescence_happy_path_real_cv_agreement(monkeypatch):
    """Both nodes report the SAME non-empty CV -> legitimate QUIESCENT."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_snapshot(p, host_map, field):
        return {n: "A:5-abc" for n in host_map}

    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "snapshot_stats_field", fake_snapshot)

    lines = waitmod.k_quiescence(params(nodes=["1a", "1b"]))
    text = "\n".join(lines)
    print(f"\n    expected: 'QUIESCENT' + 'A:5-abc'")
    print(f"    actual:   {lines}\n")
    assert "QUIESCENT" in text
    assert "A:5-abc" in text


# ============================================================================
# k_conflicts_resolved: connection error must raise, not loop to timeout
# ============================================================================

def test_k_conflicts_resolved_raises_immediately_when_node_unreachable(monkeypatch):
    """Old code: status=None became 'HTTP None' string, all_zero check failed,
    loop ran to timeout, raised 'conflicts never resolved' (lie -- network
    was the real problem).  New code: raise immediately with the right
    cause."""
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [("1a", None, "ConnectionRefusedError"),
                ("1b", None, "ConnectionRefusedError")]
    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'connection error' (NOT 'never resolved')")
    with pytest.raises(RuntimeError, match="connection error") as exc:
        waitmod.k_conflicts_resolved(params(nodes=["1a", "1b"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "never resolved" not in str(exc.value)


def test_k_conflicts_resolved_happy_path(monkeypatch):
    def fake_classify(p, nodes):
        return {n: None for n in nodes}, []

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [(t, 200, json.dumps({"TotalResults": 0, "Results": []}).encode())
                for t in targets]
    monkeypatch.setattr(waitmod, "classify_nodes", fake_classify)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    msg = waitmod.k_conflicts_resolved(params(nodes=["1a", "1b"]))
    print(f"\n    expected: 'CONFLICTS RESOLVED'")
    print(f"    actual:   {msg!r}\n")
    assert "CONFLICTS RESOLVED" in msg


# ============================================================================
# k_marker_propagation: unreachable target must raise, not silently loop
# ============================================================================

def test_k_marker_propagation_raises_on_unreachable_target(monkeypatch):
    """Source PUT succeeds, but the propagation poll sees status=None for
    every target -- network failure.  Old code: silently ignored, looped to
    timeout, raised 'marker did not propagate'.  New code: raise loud with
    the network cause."""
    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        return 201, b""       # the source PUT

    def fake_rpn(method, targets, domain, path, client_cert, ca_cert,
                 body=None, content_type=None, timeout=30):
        return [(t, None, "ConnectionRefusedError") for t in targets]

    monkeypatch.setattr(waitmod, "request", fake_request)
    monkeypatch.setattr(waitmod, "request_per_node", fake_rpn)

    print(f"\n    expected: RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        waitmod.k_marker_propagation(params(source="1a", targets=["2a", "2b"]))
    print(f"    actual:   {exc.value!s}\n")


# ============================================================================
# k_doc_count_match: orphan shard must raise, not silently exclude
# ============================================================================

def test_aggregate_count_raises_when_a_shard_has_no_members(monkeypatch):
    """Sharded layout where shard 1 has no Members (orphaned -- could be
    after chaos killed the shard's only node).  Old code silently dropped
    that shard from the total; new code raises."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        if path.endswith("/stats"):
            return 500, b'{"Type":"nodeTag is mandatory"}'
        if "/admin/databases" in path:
            return 200, json.dumps({"Sharding": {
                "Shards": {
                    "0": {"Members": ["A"]},
                    "1": {"Members": []},      # ORPHANED
                },
            }}).encode()
        if "shardNumber=0" in path:
            return 200, json.dumps({"CountOfDocuments": 7}).encode()
        return 200, _stats_body({})
    monkeypatch.setattr(waitmod, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'orphan shard'")
    with pytest.raises(RuntimeError, match="orphan shard") as exc:
        waitmod.k_doc_count_match(params(
            source="src", target="tgt", target_db_name="db1"))
    print(f"    actual:   {exc.value!s}\n")
    assert "1" in str(exc.value)


def test_aggregate_count_raises_when_top_level_stats_unreachable(monkeypatch):
    """Top-level /stats raises (connection error) -> immediate raise.  Old
    code would have crashed inside the predicate; new code raises with a
    clear message."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        raise ConnectionRefusedError("dead source")
    monkeypatch.setattr(waitmod, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'top-level /stats unreachable'")
    with pytest.raises(RuntimeError, match="top-level /stats unreachable") as exc:
        waitmod.k_doc_count_match(params(
            source="src", target="tgt", target_db_name="db1"))
    print(f"    actual:   {exc.value!s}\n")


def test_doc_count_match_happy_path_flat_cluster(monkeypatch):
    """Both source and target are flat (non-sharded), both report the same
    CountOfDocuments -> MATCHED."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, json.dumps({"CountOfDocuments": 100}).encode()
    monkeypatch.setattr(waitmod, "request", fake)

    msg = waitmod.k_doc_count_match(params(
        source="src", target="tgt", target_db_name="db1"))
    print(f"\n    expected: 'DOC COUNT MATCHED' with src=100 == tgt=100")
    print(f"    actual:   {msg!r}\n")
    assert "DOC COUNT MATCHED" in msg
    assert "src=100 == tgt=100" in msg
