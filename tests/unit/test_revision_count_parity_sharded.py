"""
Tests for the sharded-source routing in k_revision_count_parity.

What changed:        the kind now auto-detects sharded targets via GET
                     /admin/databases?name=<db> and routes per-id revisions
                     reads through the database's orchestrator member,
                     instead of hitting the caller-supplied node directly.

Pinned here:         _resolve_revisions_route returns the target itself for
                     non-sharded dbs, and the orchestrator member node for
                     sharded dbs (re-keyed onto the same cluster id).
                     A missing orchestrator on a sharded db raises loud.
                     The kind-level call labels output by the ORIGINAL target,
                     not the routing node.
"""

import json
from collections import defaultdict

import pytest

import ravendb_diagnostic as diag


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def _admin_databases_body(*, sharded, orch_members=None):
    if not sharded:
        return json.dumps({"Topology": {"Members": ["A"]}}).encode()
    # Pass `[]` explicitly to model "no orchestrator" -- `or` would collapse it
    # to the default ["A"] which is exactly the path the no-orchestrator test
    # is trying to exercise.
    if orch_members is None:
        orch_members = ["A"]
    return json.dumps({
        "Sharding": {
            "Shards": {"0": {"Members": ["A"]}, "1": {"Members": ["B"]}},
            "Orchestrator": {
                "Topology": {"Members": orch_members},
            },
        }
    }).encode()


def _revisions_body(n):
    return json.dumps({"Results": [{"@id": "u%d" % i} for i in range(n)]}).encode()


# ---- _resolve_revisions_route ---------------------------------------------

def test_resolves_non_sharded_target_to_itself(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _admin_databases_body(sharded=False)
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: non-sharded -> route == target ('2a')")
    route = diag._resolve_revisions_route(params(), "2a")
    print(f"    actual:   {route!r}\n")
    assert route == "2a"


def test_resolves_sharded_target_to_orchestrator_member_on_same_cluster(monkeypatch):
    """target '1a' belongs to cluster '1'; orchestrator member tag is 'B';
    route must be '1b' (cluster id of target + lowercase orch member)."""
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _admin_databases_body(sharded=True, orch_members=["B"])
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: sharded with orch=['B'] -> route '1b'")
    route = diag._resolve_revisions_route(params(), "1a")
    print(f"    actual:   {route!r}\n")
    assert route == "1b"


def test_raises_loud_when_sharded_db_has_no_orchestrator(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _admin_databases_body(sharded=True, orch_members=[])
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: raises RuntimeError mentioning 'no orchestrator members'")
    with pytest.raises(RuntimeError, match="no orchestrator members") as exc:
        diag._resolve_revisions_route(params(), "1a")
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_when_admin_databases_fails(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 404, b""
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: raises RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag._resolve_revisions_route(params(), "1a")
    print(f"    actual:   {exc.value!s}\n")


# ---- k_revision_count_parity end-to-end with mocked HTTP ------------------

def test_kind_routes_sharded_target_through_orchestrator_and_keeps_label(monkeypatch):
    """nodes=['1a', '2a'] -- '1a' is a sharded leader (orch=['B'] -> route '1b'),
    '2a' is plain.  Per-id /revisions GETs MUST hit '1b' (not '1a') for the
    sharded target, but the output table MUST still show '1a' alongside '2a'
    (caller-friendly label, not the internal routing node)."""
    hit = []

    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            sharded = (target == "1a")
            return 200, _admin_databases_body(
                sharded=sharded, orch_members=["B"] if sharded else None)
        if path.startswith("/databases/db1/revisions?id="):
            hit.append(target)
            return 200, _revisions_body(3)
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    lines = diag.k_revision_count_parity(
        params(nodes=["1a", "2a"], ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    expected: revisions GET hit ['1b','2a'], output table shows ['1a','2a']")
    print(f"    actual:   revisions GET hit {hit!r}")
    print(f"              header: {lines[0]!r}\n")
    assert hit == ["1b", "2a"]
    assert "['1a', '2a']" in lines[0]
    assert "PASS" in text
