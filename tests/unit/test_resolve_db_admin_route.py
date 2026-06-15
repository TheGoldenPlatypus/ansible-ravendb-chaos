"""
Tests for resolve_db_admin_route() in ravendb_client.

What the helper does:
    Given (target, db_name), inspect /admin/databases?name=<db> on target.
    For non-sharded dbs, return target unchanged.  For sharded dbs, return
    the container name of the database's first orchestrator member -- so
    callers can send admin/<db>/ requests to a node that won't return
    HTTP 410 DatabaseNotRelevantException.

Pinned here:
    * non-sharded returns the original target
    * sharded re-routes to "<cluster_id><orch_member_lowercased>"
    * cluster_id is correctly preserved across target-id changes
      (e.g. target='62a' + orch=['G'] -> '62g', not '1g')
    * /admin/databases transport failure -> RuntimeError mentioning 'unreachable'
    * /admin/databases non-200 -> RuntimeError mentioning the HTTP code
    * sharded db with no orchestrator -> RuntimeError loud
"""

import json

import pytest

import ravendb_client as rc


def _admin_db_body(*, sharded, orch_members=None):
    if not sharded:
        return json.dumps({"Topology": {"Members": ["A"]}}).encode()
    # `[]` for orch_members is treated as literal (no orchestrator).
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


def test_non_sharded_returns_target_unchanged(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _admin_db_body(sharded=False)
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(rc, "request", fake_request)

    out = rc.resolve_db_admin_route("2a", "db1", "ignored", "", "")

    print(f"\n    expected: non-sharded route stays '2a'")
    print(f"    actual:   {out!r}\n")
    assert out == "2a"


def test_sharded_routes_to_orchestrator_member_on_same_cluster(monkeypatch):
    """target='62a' + orch=['G'] -> '62g'.  The cluster id (62) is preserved
    from the target -- crucial when the lab runs on a non-default cluster
    id range (parallel overnight runs).  Previously a literal '1g' here was
    the bug shape that hit RV-3 at cluster_id_start=60."""
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _admin_db_body(sharded=True, orch_members=["G"])
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(rc, "request", fake_request)

    out = rc.resolve_db_admin_route("62a", "db1", "ignored", "", "")

    print(f"\n    expected: sharded with orch=['G'] on target '62a' -> route '62g'")
    print(f"    actual:   {out!r}\n")
    assert out == "62g"


def test_sharded_keeps_two_digit_cluster_id_intact(monkeypatch):
    """target like '999z' + orch=['C'] -> '999c'.  cluster_id slicing is
    target[:-1], so multi-digit prefixes survive."""
    def fake_request(method, target, domain, path, *a, **kw):
        return 200, _admin_db_body(sharded=True, orch_members=["C"])
    monkeypatch.setattr(rc, "request", fake_request)

    out = rc.resolve_db_admin_route("999z", "db1", "ignored", "", "")
    assert out == "999c"


def test_raises_loud_on_no_orchestrator(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        return 200, _admin_db_body(sharded=True, orch_members=[])
    monkeypatch.setattr(rc, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'no orchestrator members'")
    with pytest.raises(RuntimeError, match="no orchestrator members") as exc:
        rc.resolve_db_admin_route("62a", "db1", "ignored", "", "")
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_on_admin_databases_non_200(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        return 404, b""
    monkeypatch.setattr(rc, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        rc.resolve_db_admin_route("62a", "db1", "ignored", "", "")
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_on_transport_failure(monkeypatch):
    """If /admin/databases itself can't be reached (DNS / refused / timeout)
    we MUST surface a loud error -- silently routing to the supplied target
    would just produce a 410 downstream which the caller can't disambiguate."""
    def fake_request(method, target, domain, path, *a, **kw):
        raise ConnectionRefusedError("simulated")
    monkeypatch.setattr(rc, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        rc.resolve_db_admin_route("62a", "db1", "ignored", "", "")
    print(f"    actual:   {exc.value!s}\n")
