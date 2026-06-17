"""
Tests for k_shard_placement_check.

What the kind does:  asserts each probe doc id is present on EXACTLY ONE
                     shard of a sharded database.  Reads /admin/databases to
                     enumerate shards, then per-shard /docs?id=...&shardNumber=N
                     for each id.

Pinned here:         passes when each id has exactly one owner shard;
                     fails (under assert_mode) when an id is missing on all
                     shards or duplicated across shards; raises loud on a
                     non-sharded db, on /admin/databases failure, and on
                     transport errors during a per-shard probe.
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


# Map tag -> shard_id.  Test setup: 3 shards each with a single member tag.
# Shard "0" -> tag "A", "1" -> "B", "2" -> "C".  k_shard_placement_check now
# probes via the orchestrator with ?nodeTag=<X>, so the test maps the tag back
# to a shard id.
_TAG_TO_SHARD = {"A": "0", "B": "1", "C": "2"}


def _sharded_admin_body(shard_ids=("0", "1", "2")):
    members_by_shard = {"0": ["A"], "1": ["B"], "2": ["C"]}
    return json.dumps({
        "Sharding": {
            "Shards": {s: {"Members": members_by_shard[s]} for s in shard_ids},
            "Orchestrator": {"Topology": {"Members": ["A"]}},
        }
    }).encode()


def _docs_body(present):
    """200-with-results when present; otherwise empty Results."""
    if present:
        return json.dumps({"Results": [{"@id": "u"}]}).encode()
    return json.dumps({"Results": []}).encode()


def _shard_from_path(path):
    """Pull shard id from /databases/db1/docs?id=X&nodeTag=<tag> by mapping
    the tag back to a shard via _TAG_TO_SHARD."""
    for part in path.split("&"):
        if part.startswith("nodeTag="):
            tag = part.split("=", 1)[1]
            return _TAG_TO_SHARD[tag]
    raise AssertionError("no nodeTag in %r" % path)


def _id_from_path(path):
    for part in path.split("&"):
        if part.startswith("id=") or path.split("?", 1)[1].startswith("id="):
            return path.split("id=", 1)[1].split("&", 1)[0]
    raise AssertionError("no id in %r" % path)


def _build_request(placement):
    """Build a fake request() that pretends each id lives on the shards listed
    in `placement` ({doc_id: [shard_id_str, ...]}).  Handles BOTH the
    /admin/databases lookup (sharding info) AND the per-shard docs probe."""
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _sharded_admin_body()
        if path.startswith("/databases/db1/docs?"):
            doc_id = _id_from_path(path)
            shard = _shard_from_path(path)
            present = shard in placement.get(doc_id, [])
            return 200, _docs_body(present)
        raise AssertionError("unexpected path %r" % path)
    return fake_request


# ---- happy path -----------------------------------------------------------

def test_passes_when_each_id_lives_on_exactly_one_shard(monkeypatch):
    placement = {"users/0": ["0"], "users/1": ["1"], "users/2": ["2"]}
    monkeypatch.setattr(diag, "request", _build_request(placement))

    lines = diag.k_shard_placement_check(
        params(target="1a", ids=list(placement)))
    text = "\n".join(lines)

    print(f"\n    expected: PASS, every id owners=[<one shard>]")
    for ln in lines:
        print(f"        {ln}")
    print()
    assert "missing(0-shard): 0   duplicate(>1-shard): 0" in text
    assert "PASS  every probe id lives on exactly one shard" in text


# ---- failure modes --------------------------------------------------------

def test_fails_when_id_is_missing_on_all_shards(monkeypatch):
    placement = {"users/0": ["0"], "users/1": []}    # users/1 nowhere
    monkeypatch.setattr(diag, "request", _build_request(placement))

    print(f"\n    expected: FAIL ... missing on: ['users/1']")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_shard_placement_check(
            params(target="1a", ids=list(placement), assert_mode=True))
    last = exc.value.lines[-1]
    print(f"    actual:   {last!r}\n")
    assert "FAIL" in last
    assert "users/1" in last


def test_fails_when_id_is_duplicated_across_shards(monkeypatch):
    placement = {"users/0": ["0", "1"]}              # owner on 0 AND 1
    monkeypatch.setattr(diag, "request", _build_request(placement))

    print(f"\n    expected: FAIL ... duplicate on: ['users/0', ['0', '1']]")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_shard_placement_check(
            params(target="1a", ids=list(placement), assert_mode=True))
    last = exc.value.lines[-1]
    print(f"    actual:   {last!r}\n")
    assert "FAIL" in last
    assert "users/0" in last


# ---- input guards ---------------------------------------------------------

def test_raises_loud_on_non_sharded_db(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, json.dumps({"Topology": {"Members": ["A"]}}).encode()
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'not a sharded database'")
    with pytest.raises(RuntimeError, match="not a sharded database") as exc:
        diag.k_shard_placement_check(params(target="2a", ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_on_admin_databases_failure(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 404, b""
        raise AssertionError("unexpected path %r" % path)
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_shard_placement_check(params(target="1a", ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_on_transport_error_during_probe(monkeypatch):
    """Per-shard probe transport error must NOT silently drop the shard from
    the owner set (that would let a duplicate-placement bug masquerade as a
    correct placement).  Fail loud."""
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/admin/databases?name="):
            return 200, _sharded_admin_body()
        raise ConnectionError("simulated transport failure")
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'transport error during placement probe'")
    with pytest.raises(RuntimeError, match="transport error during placement probe") as exc:
        diag.k_shard_placement_check(params(target="1a", ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")
