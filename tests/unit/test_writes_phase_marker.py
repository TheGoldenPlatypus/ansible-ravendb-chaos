"""
Tests for the optional `phase` cohort marker on doc-write kinds.

What the param does:  when `phase` is set, every body created by k_docs,
                      k_docs_revisions, k_docs_freeform, k_docs_interleaved
                      includes a top-level `"Phase": <value>` field.  Scenarios
                      that toggle behavior mid-run (e.g. enabling a feature
                      flag) use this to distinguish docs written before vs
                      after the toggle when verifying downstream.

Pinned here:
  - phase=None / unset: body has NO "Phase" field (backward-compat)
  - phase="pre_cv_toggle": every body PUT carries Phase: "pre_cv_toggle"
  - same for k_docs_revisions across both revs and docs
  - same for k_docs_freeform and k_docs_interleaved
  - the marker is at the top level of the body, NOT inside @metadata
"""

import json
from collections import defaultdict

import pytest

import ravendb_writes as writes


def _params(**overrides):
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["target"] = "1a"
    for k, v in overrides.items():
        p[k] = v
    return p


def _capture_bodies(monkeypatch):
    """Patch request + put_idempotent to capture every PUT body posted."""
    captured = []
    def fake_request(method, target, domain, path,
                     client_cert, ca_cert, body=None, **kw):
        if method == "PUT":
            captured.append(body)
        return 201, b""
    def fake_put_idempotent(p, target, path, body):
        captured.append(body)
        return 201, b""
    monkeypatch.setattr(writes, "request", fake_request)
    monkeypatch.setattr(writes, "put_idempotent", fake_put_idempotent)
    return captured


def test_k_docs_omits_phase_by_default(monkeypatch):
    captured = _capture_bodies(monkeypatch)
    writes.k_docs(_params(count=3, id_prefix="users/sink1"))
    assert all("Phase" not in b for b in captured)


def test_k_docs_writes_phase_when_set(monkeypatch):
    captured = _capture_bodies(monkeypatch)
    writes.k_docs(_params(count=3, id_prefix="users/sink1",
                           phase="pre_cv_toggle"))
    assert len(captured) == 3
    for body in captured:
        assert body.get("Phase") == "pre_cv_toggle", \
            "every body must carry Phase=pre_cv_toggle, got %r" % body
        # Marker is top-level, not inside @metadata
        assert "Phase" not in (body.get("@metadata") or {})


def test_k_docs_revisions_writes_phase_on_every_revision(monkeypatch):
    captured = _capture_bodies(monkeypatch)
    writes.k_docs_revisions(_params(
        count=2, revs_per_doc=3, id_prefix="users/sink1",
        phase="post_cv_toggle"))
    # 2 docs x 3 revs = 6 PUTs, all must carry the marker
    assert len(captured) == 6
    assert all(b.get("Phase") == "post_cv_toggle" for b in captured)


def test_k_docs_freeform_writes_phase_when_set(monkeypatch):
    captured = _capture_bodies(monkeypatch)
    writes.k_docs_freeform(_params(count=2, phase="pre_cv_toggle"))
    assert len(captured) == 2
    assert all(b.get("Phase") == "pre_cv_toggle" for b in captured)


def test_k_docs_interleaved_writes_phase_when_set(monkeypatch):
    captured = _capture_bodies(monkeypatch)
    writes.k_docs_interleaved(_params(
        count=4, prefixes=["users/sink1/active", "users/sink2/active"],
        phase="post_cv_toggle"))
    assert len(captured) == 4
    assert all(b.get("Phase") == "post_cv_toggle" for b in captured)


def test_phase_marker_lives_at_top_level_not_in_metadata(monkeypatch):
    """Regression guard: the marker must remain a top-level body field, not
    a metadata sub-key.  Top-level keeps it visible in Studio at a glance
    without expanding the @metadata blob."""
    captured = _capture_bodies(monkeypatch)
    writes.k_docs(_params(count=1, id_prefix="x", phase="P1"))
    body = captured[0]
    assert "Phase" in body
    assert body["Phase"] == "P1"
    assert "Phase" not in body["@metadata"]
