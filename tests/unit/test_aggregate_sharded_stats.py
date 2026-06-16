"""
Tests for aggregate_sharded_stats targeting the SHARD OWNER, not the original
target (the orchestrator).

What the helper does:  for a sharded DB, fetch each shard's /stats from a
                       node that actually owns the shard, then sum integer
                       fields across all three.

Why this matters:  the cursor 7.x per-shard endpoint serves only LOCAL shard
                   data.  Asking the orchestrator for shard N's stats returns
                   410, even via /databases/<db>$N/stats.  Old code probed
                   `target` (the orchestrator) for every shard and only got
                   the one shard `target` happened to own locally -- a 3-shard
                   DB aggregated to 1/3 of the true total.  This test pins
                   that we now route each per-shard probe to a member node.
"""

from collections import defaultdict

import ravendb_diagnostic as diag


def _params():
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    return p


def test_aggregate_routes_each_shard_to_its_owner(monkeypatch):
    """Sharded DB on cluster '62': shard 0 = [A,B,C], shard 1 = [D,E,F],
    shard 2 = [G,H,I].  When asked from orchestrator 62a, the aggregator
    must hit 62a for shard 0, 62d for shard 1, 62g for shard 2 -- NOT
    62a for all three (which is what the buggy version did)."""
    db_record_body = (
        '{"Sharding":{"Shards":{'
        '"0":{"Members":["A","B","C"]},'
        '"1":{"Members":["D","E","F"]},'
        '"2":{"Members":["G","H","I"]}'
        '}}}'
    ).encode()

    request_calls = []
    probe_calls = []

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        request_calls.append((target, path))
        if "/admin/databases" in path:
            return 200, db_record_body
        return 410, b""

    # probe_shard_endpoint records (target, shard) and returns per-shard
    # CountOfDocuments mimicking real shard sizes that sum to 20000.
    per_shard_docs = {"0": 6721, "1": 6700, "2": 6579}

    def fake_probe(target, domain, db, tag, shard_id,
                   client_cert, ca_cert, suffix="stats", timeout=30):
        probe_calls.append((target, shard_id))
        body = ('{"CountOfDocuments":%d,"DatabaseId":"x"}'
                % per_shard_docs[shard_id]).encode()
        return 200, body

    monkeypatch.setattr(diag, "request", fake_request)
    monkeypatch.setattr(diag, "probe_shard_endpoint", fake_probe)

    out = diag.aggregate_sharded_stats(_params(), "62a", "db1")

    print("\n    expected: probe routed to 62a, 62d, 62g (one per shard owner)")
    print("    actual:   %s" % probe_calls)
    assert out is not None
    assert out["CountOfDocuments"] == sum(per_shard_docs.values()), \
        f"aggregate should be {sum(per_shard_docs.values())} but got {out['CountOfDocuments']}"

    targets_used = sorted(t for t, _ in probe_calls)
    assert targets_used == ["62a", "62d", "62g"], \
        f"each shard must be probed at its owner, got: {targets_used}"


def test_aggregate_skips_orphan_shard_silently(monkeypatch):
    """A shard with empty Members list should be silently skipped -- the
    aggregator doesn't have a node to route to, so it returns partial data
    rather than crashing.  Caller can detect orphans separately."""
    db_record_body = (
        '{"Sharding":{"Shards":{'
        '"0":{"Members":["A"]},'
        '"1":{"Members":[]},'             # orphan
        '"2":{"Members":["G"]}'
        '}}}'
    ).encode()

    def fake_request(method, target, domain, path, *a, **kw):
        if "/admin/databases" in path:
            return 200, db_record_body
        return 410, b""

    probed = []

    def fake_probe(target, domain, db, tag, shard_id, *a, **kw):
        probed.append(shard_id)
        return 200, b'{"CountOfDocuments":100}'

    monkeypatch.setattr(diag, "request", fake_request)
    monkeypatch.setattr(diag, "probe_shard_endpoint", fake_probe)

    out = diag.aggregate_sharded_stats(_params(), "62a", "db1")

    assert out["CountOfDocuments"] == 200
    assert sorted(probed) == ["0", "2"], "orphan shard 1 must be silently skipped"


def test_aggregate_returns_none_on_admin_databases_failure(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        return 500, b'"down"'

    monkeypatch.setattr(diag, "request", fake_request)
    assert diag.aggregate_sharded_stats(_params(), "62a", "db1") is None


def test_aggregate_returns_none_on_non_sharded_db(monkeypatch):
    """Caller (get_stats) only calls aggregate when sharded; defense in
    depth: if Sharding.Shards is empty, return None so callers see
    'no stats' rather than '0 docs across 0 shards'."""
    def fake_request(method, target, domain, path, *a, **kw):
        if "/admin/databases" in path:
            return 200, b'{"Topology":{"Members":["A"]}}'   # no Sharding
        return 410, b""

    monkeypatch.setattr(diag, "request", fake_request)
    assert diag.aggregate_sharded_stats(_params(), "62a", "db1") is None
