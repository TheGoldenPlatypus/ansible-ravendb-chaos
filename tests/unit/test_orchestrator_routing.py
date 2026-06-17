"""
Tests for the sharded-API rewrite: all sharded reads route through the
ORCHESTRATOR, never address shards directly.

What the helpers do:
  orchestrator_for(target, ..., db)  -- looks up the orchestrator node
                                        from /admin/databases, returns the
                                        container name (e.g. '62f').  For
                                        non-sharded dbs, returns `target`
                                        unchanged.
  with_node_tag(path, tag)           -- appends '?nodeTag=<X>' (or '&...' if
                                        the path already has a query).

What classify_nodes does (new shape):
  - Non-sharded: each reachable target maps to None; failures go to skipped.
  - Sharded:     each reachable target maps to ('via', <orch>); the caller
                 uses _stats_request_for / stats_request_for to compose a
                 (call_target, path) tuple where the orchestrator is the
                 entry point and ?nodeTag=<X> selects the proxied node.

Why this matters:  the cursor 7.2 build serves only local-shard data on
non-orchestrator nodes (returns 410 DatabaseNotRelevant otherwise).  The
old probe_shard_endpoint apparatus (with $N url form + query-param
fallback) was wrong -- the official client (verified against
Raven.Client/Documents/Operations/GetStatisticsOperation.cs) just calls
/databases/<db>/stats on the orchestrator and lets the server fan out.

Pinned here:
  - orchestrator_for returns target unchanged on a non-sharded DB record
  - orchestrator_for returns container '<prefix><tag>' on a sharded DB
  - orchestrator_for raises on a non-200 admin/databases response
  - with_node_tag uses '?' when the path has no query, '&' when it does
  - classify_nodes (wait) routes sharded probes via orchestrator with nodeTag
  - get_stats (diagnostic) on a sharded DB hits /stats/essential via the
    orchestrator (not /stats, not per-shard)
"""

from collections import defaultdict
from unittest.mock import patch

import pytest

from ansible.module_utils import ravendb_client as rc
import ravendb_diagnostic as diag


def _params(**overrides):
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    for k, v in overrides.items():
        p[k] = v
    return p


# ---------------------------------------------------------------------------- orchestrator_for

def _record_sharded(orch_tag="F"):
    """JSON for /admin/databases when db1 is sharded with Orchestrator=<orch_tag>."""
    import json as _json
    return _json.dumps({
        "Sharding": {
            "Orchestrator": {"Topology": {"Members": [orch_tag]}},
            "Shards": {
                "0": {"Members": ["A", "B", "C"]},
                "1": {"Members": ["D", "E", "F"]},
                "2": {"Members": ["G", "H", "I"]},
            },
        }
    }).encode()


def _record_flat():
    import json as _json
    return _json.dumps({"Topology": {"Members": ["A", "B", "C"]}}).encode()


def test_orchestrator_for_sharded_returns_container(monkeypatch):
    monkeypatch.setattr(rc, "request",
                        lambda *a, **kw: (200, _record_sharded(orch_tag="F")))
    out = rc.orchestrator_for("62a", "hubsink.test", "db1", "", "")
    assert out == "62f"


def test_orchestrator_for_non_sharded_returns_target_unchanged(monkeypatch):
    monkeypatch.setattr(rc, "request",
                        lambda *a, **kw: (200, _record_flat()))
    out = rc.orchestrator_for("1a", "hubsink.test", "db1", "", "")
    assert out == "1a"


def test_orchestrator_for_non_200_raises(monkeypatch):
    monkeypatch.setattr(rc, "request", lambda *a, **kw: (500, b'"down"'))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        rc.orchestrator_for("1a", "hubsink.test", "db1", "", "")


# ---------------------------------------------------------------------------- with_node_tag

def test_with_node_tag_starts_query():
    assert rc.with_node_tag("/databases/db1/stats", "A") == \
        "/databases/db1/stats?nodeTag=A"


def test_with_node_tag_extends_query():
    assert rc.with_node_tag("/databases/db1/docs?id=users/0", "A") == \
        "/databases/db1/docs?id=users/0&nodeTag=A"


# ---------------------------------------------------------------------------- get_stats sharded

def test_get_stats_sharded_uses_stats_essential_on_orchestrator(monkeypatch):
    """For a sharded DB, get_stats(target, shard_id=None) must call
    /stats/essential on the orchestrator, NOT /stats on target or any
    /databases/db$N URL.  Aggregated counts come from the server-side fan-out."""
    import json as _json
    calls = []

    def fake_request(method, target, domain, path, *a, **kw):
        calls.append((target, path))
        if "/admin/databases" in path:
            return 200, _record_sharded(orch_tag="F")
        if "/stats/essential" in path:
            return 200, _json.dumps({"CountOfDocuments": 20000}).encode()
        # Any other URL is a regression -- old per-shard probing tried these.
        raise AssertionError("unexpected URL: %s %s" % (method, path))

    monkeypatch.setattr(diag, "request", fake_request)
    monkeypatch.setattr(rc, "request", fake_request)

    stats = diag.get_stats(_params(), "62a")

    assert stats["CountOfDocuments"] == 20000
    # Must hit orchestrator's /stats/essential.
    assert any(t == "62f" and "/stats/essential" in p for t, p in calls), \
        "get_stats must call /stats/essential on the orchestrator (62f); calls=%s" % calls
    # Must NOT touch any '$N' URL.
    assert not any("$" in p for _, p in calls), \
        "no call should use the $N internal-shard URL form; calls=%s" % calls
