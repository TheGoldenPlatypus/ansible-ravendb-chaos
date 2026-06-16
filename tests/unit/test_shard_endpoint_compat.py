"""
Tests for the sharded-endpoint compat shim in module_utils/ravendb_client.

What the helpers do:
  - shard_endpoint_path(db, shard, suffix)        -> 7.x form  /databases/<db>$<N>/<suffix>
  - shard_endpoint_path_legacy(db, tag, shard, s) -> 6.x form  /databases/<db>/<s>?nodeTag&shardNumber
  - probe_shard_endpoint(...)                     -> try 7.x first, fall back to 6.x on non-200

Why the shim exists:  the cursor v_new build returns 410 DatabaseNotRelevant
on /databases/<db>/stats?nodeTag=&shardNumber= (the 6.x form).  Mid-rolling-
upgrade clusters contain BOTH builds, so callers need to try both URL forms.

Pinned here:
  - 7.x URL shape:           /databases/db1$0/stats
  - 6.x URL shape:           /databases/db1/stats?nodeTag=A&shardNumber=0
  - suffix with '?':         legacy URL uses '&' not '?' for the extra params
  - probe happy path on 7.x: one call, 200 returned, fallback NOT triggered
  - probe fallback to 6.x:   first call returns non-200, second call returns 200
  - probe failure surface:   both calls non-200, last response returned (not silently
                             swallowed)
"""

from collections import defaultdict

import pytest

from ansible.module_utils import ravendb_client as rc


# ---------------------------------------------------------------------------- URL builders

def test_new_form_is_dollar_suffix():
    assert rc.shard_endpoint_path("db1", "0") == "/databases/db1$0/stats"
    assert rc.shard_endpoint_path("db1", "0", suffix="stats") == "/databases/db1$0/stats"


def test_new_form_with_docs_suffix():
    # The shim's other caller: shard_placement_check probes /docs?id=...
    p = rc.shard_endpoint_path("db1", "2", suffix="docs?id=users/0")
    assert p == "/databases/db1$2/docs?id=users/0"


def test_legacy_form_uses_question_mark_for_first_param():
    p = rc.shard_endpoint_path_legacy("db1", "A", "0")
    assert p == "/databases/db1/stats?nodeTag=A&shardNumber=0"


def test_legacy_form_uses_ampersand_when_suffix_already_has_query():
    # When suffix already contains '?', the nodeTag/shardNumber must join with '&'.
    p = rc.shard_endpoint_path_legacy("db1", "A", "0", suffix="docs?id=users/0")
    assert p == "/databases/db1/docs?id=users/0&nodeTag=A&shardNumber=0"


# ---------------------------------------------------------------------------- probe

def _make_fake_request(responses):
    """Build a request() stand-in whose call N returns responses[N].  Each
    entry is (status, body)."""
    calls = []
    iterator = iter(responses)

    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        calls.append({"method": method, "path": path})
        return next(iterator)
    return fake, calls


def test_probe_returns_first_call_when_new_form_hits_200(monkeypatch):
    fake, calls = _make_fake_request([(200, b'{"CountOfDocuments":7}')])
    monkeypatch.setattr(rc, "request", fake)

    status, body = rc.probe_shard_endpoint(
        "1d", "hubsink.test", "db1", "D", "1", "", "")

    assert status == 200
    assert b"CountOfDocuments" in body
    assert len(calls) == 1, "fallback fired when first call already succeeded"
    assert calls[0]["path"] == "/databases/db1$1/stats"


def test_probe_falls_back_to_legacy_on_non_200(monkeypatch):
    # 7.x form returns 410 (cursor build edge case) -> fall back to 6.x form.
    fake, calls = _make_fake_request([
        (410, b'{"Type":"...DatabaseNotRelevantException..."}'),
        (200, b'{"CountOfDocuments":7}'),
    ])
    monkeypatch.setattr(rc, "request", fake)

    status, body = rc.probe_shard_endpoint(
        "1d", "hubsink.test", "db1", "D", "1", "", "")

    assert status == 200
    assert b"CountOfDocuments" in body
    assert len(calls) == 2
    assert calls[0]["path"] == "/databases/db1$1/stats"
    assert calls[1]["path"] == "/databases/db1/stats?nodeTag=D&shardNumber=1"


def test_probe_surfaces_last_failure_when_both_forms_fail(monkeypatch):
    # If even the legacy fallback returns non-200, surface that final status
    # rather than silently returning 200 from somewhere.
    fake, calls = _make_fake_request([
        (410, b'"DatabaseNotRelevantException"'),
        (500, b'"InternalServerError"'),
    ])
    monkeypatch.setattr(rc, "request", fake)

    status, body = rc.probe_shard_endpoint(
        "1d", "hubsink.test", "db1", "D", "1", "", "")

    assert status == 500
    assert b"InternalServerError" in body
    assert len(calls) == 2


def test_probe_passes_suffix_through_to_both_forms(monkeypatch):
    # Same docs?id=X suffix should drive different URL shapes on the two forms.
    fake, calls = _make_fake_request([
        (410, b""),
        (200, b'{"Results":[{"@metadata":{"@id":"users/0"}}]}'),
    ])
    monkeypatch.setattr(rc, "request", fake)

    status, _ = rc.probe_shard_endpoint(
        "1d", "hubsink.test", "db1", "D", "1", "", "",
        suffix="docs?id=users%2F0")

    assert status == 200
    assert calls[0]["path"] == "/databases/db1$1/docs?id=users%2F0"
    assert calls[1]["path"] == "/databases/db1/docs?id=users%2F0&nodeTag=D&shardNumber=1"
