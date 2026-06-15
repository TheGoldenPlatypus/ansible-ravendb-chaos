"""
Tests for classify_nodes' handling of RavenDB 7.x sharded /stats response.

RavenDB 6.x returned HTTP 500 with 'nodeTag is mandatory' when /stats was
hit on a shard-only member of a sharded database.  7.x returns HTTP 410
DatabaseNotRelevantException for the same situation.  classify_nodes must
treat BOTH as "this node hosts a shard, resolve the per-shard route" --
not as "skipped".

If 7.x's 410 falls through to `skipped`, every shard-only probe disappears
from host_map, downstream wait-kinds see empty host_map and raise
"no probed node has database 'X'".  Hit on RV-3 step 4 when draining
shard Sy (whose 3 replicas are all shard-only members of 'db1').
"""

import json

import pytest

import ravendb_wait as waitmod
import ravendb_diagnostic as diagmod


_DB_NOT_RELEVANT_BODY = (
    b'{"Url":"/databases/db1/stats",'
    b'"Type":"Raven.Client.Exceptions.Database.DatabaseNotRelevantException",'
    b'"Message":"Cant get or add orchestrator for database db1 because it is '
    b'not relevant on this node Y"}'
)


def _params():
    """Minimal params dict shared by both modules' classify_nodes."""
    return {
        "db_name": "db1",
        "ravendb_domain": "ignored",
        "client_cert": "",
        "ca_cert": "",
    }


@pytest.mark.parametrize("mod_name, mod", [
    ("ravendb_wait", waitmod),
    ("ravendb_diagnostic", diagmod),
])
def test_classify_nodes_treats_7x_410_as_sharded_member(mod_name, mod, monkeypatch):
    """A node that responds with 410 DatabaseNotRelevantException to /stats
    must be classified as a sharded member -- routed via per-shard /stats?
    nodeTag=X&shardNumber=N rather than dumped into 'skipped'."""

    # Two probes:
    #   62a -- returns 410 DatabaseNotRelevant (shard-only member, 7.x shape)
    #   62b -- returns 410 DatabaseNotRelevant (same)
    def fake_request_per_node(method, targets, domain, path, *a, **kw):
        return [(t, 410, _DB_NOT_RELEVANT_BODY) for t in targets]

    # Shard lookup: 62a is in shard 0, 62b is in shard 1.
    def fake_resolve_shard_for_tag(p, target, tag):
        return {"A": "0", "B": "1"}.get(tag)

    # Per-shard probe: returns 200 (the routed call succeeds).
    def fake_request(method, target, domain, path, *a, **kw):
        if "shardNumber=" in path:
            return 200, b'{"CountOfDocuments": 100}'
        raise AssertionError("unexpected path %r" % path)

    monkeypatch.setattr(mod, "request_per_node", fake_request_per_node)
    monkeypatch.setattr(mod, "_resolve_shard_for_tag", fake_resolve_shard_for_tag)
    monkeypatch.setattr(mod, "request", fake_request)

    host_map, skipped = mod.classify_nodes(_params(), ["62a", "62b"])

    print(f"\n    [{mod_name}] expected: host_map={{'62a': '0', '62b': '1'}}, skipped=[]")
    print(f"    [{mod_name}] actual:   host_map={host_map}  skipped={skipped}\n")
    assert host_map == {"62a": "0", "62b": "1"}, "shard-only members fell into 'skipped'"
    assert skipped == []


@pytest.mark.parametrize("mod_name, mod", [
    ("ravendb_wait", waitmod),
    ("ravendb_diagnostic", diagmod),
])
def test_classify_nodes_still_handles_6x_500_message(mod_name, mod, monkeypatch):
    """Legacy 6.x 'nodeTag is mandatory' must still trigger the same code
    path -- the 7.x fix is additive, not a replacement."""
    body_6x = b'{"Message": "Could not retrieve database stats: nodeTag is mandatory ..."}'

    def fake_request_per_node(method, targets, domain, path, *a, **kw):
        return [(t, 500, body_6x) for t in targets]

    def fake_resolve_shard_for_tag(p, target, tag):
        return "0"

    def fake_request(method, target, domain, path, *a, **kw):
        return 200, b'{"CountOfDocuments": 50}'

    monkeypatch.setattr(mod, "request_per_node", fake_request_per_node)
    monkeypatch.setattr(mod, "_resolve_shard_for_tag", fake_resolve_shard_for_tag)
    monkeypatch.setattr(mod, "request", fake_request)

    host_map, skipped = mod.classify_nodes(_params(), ["1a"])

    print(f"\n    [{mod_name}] expected: legacy 6.x sharded marker still routed")
    print(f"    [{mod_name}] actual:   host_map={host_map}  skipped={skipped}\n")
    assert host_map == {"1a": "0"}
    assert skipped == []


@pytest.mark.parametrize("mod_name, mod", [
    ("ravendb_wait", waitmod),
    ("ravendb_diagnostic", diagmod),
])
def test_classify_nodes_skips_unrelated_410(mod_name, mod, monkeypatch):
    """A 410 that ISN'T DatabaseNotRelevant (e.g. the resource really is gone)
    must still land in `skipped` -- we don't want to mis-route random 410s
    into the sharded recovery path."""
    body_unrelated_410 = b'{"Message": "Some other thing went away"}'

    def fake_request_per_node(method, targets, domain, path, *a, **kw):
        return [(t, 410, body_unrelated_410) for t in targets]

    monkeypatch.setattr(mod, "request_per_node", fake_request_per_node)
    # _resolve_shard_for_tag and request shouldn't be called -- assert if they are
    monkeypatch.setattr(mod, "_resolve_shard_for_tag",
                        lambda *a, **kw: pytest.fail("must not enter sharded path"))

    host_map, skipped = mod.classify_nodes(_params(), ["7q"])

    print(f"\n    [{mod_name}] expected: unrelated 410 -> skipped (no shard route)")
    print(f"    [{mod_name}] actual:   host_map={host_map}  skipped={skipped}\n")
    assert host_map == {}
    assert skipped == ["7q"]
