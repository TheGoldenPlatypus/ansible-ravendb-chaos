"""
Tests for put_idempotent() -- the small bounded-retry helper used by the
seed paths (k_docs, k_docs_revisions) to absorb transient 5xx from RavenDB's
transaction-merger backpressure during sustained PUT bursts.

Pinned here:
  - 200/201 returns unchanged on first call (no retry)
  - 500/502/503/504 trigger a retry, eventual 200 returns the 200
  - 5xx persisting through the retry budget returns the final 5xx (callers
    still see the failure -- no silent swallow)
  - 4xx (client error like 400/404/409) is NOT retried -- those are real
    client-side bugs the caller must see immediately
"""

from collections import defaultdict
from unittest.mock import patch

import pytest

import ravendb_writes as wm


def params():
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    return p


def test_returns_immediately_on_201(monkeypatch):
    calls = {"n": 0}
    def fake_request(*a, **kw):
        calls["n"] += 1
        return 201, b"ok"
    monkeypatch.setattr(wm, "request", fake_request)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    status, _ = wm.put_idempotent(params(), "1a", "/p", {"v": 1})

    print(f"\n    expected: 1 call, status=201")
    print(f"    actual:   calls={calls['n']}  status={status}\n")
    assert calls["n"] == 1
    assert status == 201


def test_retries_on_500_then_succeeds(monkeypatch):
    """The motivating case: server returns 500 once, then 201 on retry."""
    sequence = iter([500, 201])
    calls = {"n": 0}
    def fake_request(*a, **kw):
        calls["n"] += 1
        return next(sequence), b""
    monkeypatch.setattr(wm, "request", fake_request)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    status, _ = wm.put_idempotent(params(), "1a", "/p", {"v": 1})

    print(f"\n    expected: 2 calls, final status=201")
    print(f"    actual:   calls={calls['n']}  status={status}\n")
    assert calls["n"] == 2
    assert status == 201


@pytest.mark.parametrize("transient", [500, 502, 503, 504])
def test_retries_on_each_transient_status(transient, monkeypatch):
    """Every status in _TRANSIENT_HTTP_STATUSES must trigger a retry."""
    sequence = iter([transient, 200])
    def fake_request(*a, **kw):
        return next(sequence), b""
    monkeypatch.setattr(wm, "request", fake_request)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    status, _ = wm.put_idempotent(params(), "1a", "/p", {"v": 1})
    assert status == 200, f"did not retry on transient {transient}"


def test_does_not_retry_on_4xx(monkeypatch):
    """4xx is a real client-side error.  Retrying it would silently mask
    bad-request bugs and slow down their surface time."""
    calls = {"n": 0}
    def fake_request(*a, **kw):
        calls["n"] += 1
        return 400, b"bad request"
    monkeypatch.setattr(wm, "request", fake_request)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    status, body = wm.put_idempotent(params(), "1a", "/p", {"v": 1})

    print(f"\n    expected: 1 call only (no retry on 4xx), status=400")
    print(f"    actual:   calls={calls['n']}  status={status}\n")
    assert calls["n"] == 1
    assert status == 400


def test_exhausts_retries_and_surfaces_final_5xx(monkeypatch):
    """If the server stays in 5xx for the entire retry budget, the kind
    must return the final 5xx so the caller sees the failure -- not a
    silent success or a different status."""
    calls = {"n": 0}
    def fake_request(*a, **kw):
        calls["n"] += 1
        return 503, b"still busy"
    monkeypatch.setattr(wm, "request", fake_request)
    monkeypatch.setattr(wm.time, "sleep", lambda _s: None)

    status, _ = wm.put_idempotent(params(), "1a", "/p", {"v": 1})

    expected_attempts = wm._HTTP_RETRY_MAX + 1
    print(f"\n    expected: {expected_attempts} attempts, final status=503")
    print(f"    actual:   calls={calls['n']}  status={status}\n")
    assert calls["n"] == expected_attempts
    assert status == 503
