"""
Tests for ravendb_client.request() resilience hardening.

What changed:
  1. SSL context is now cached per (ca_cert, client_cert) pair instead of
     being recreated per call.  A 50k-doc seed used to re-parse both PEM
     files 50k times; now it parses them once and reuses the context.
  2. `Connection: close` is set on every outgoing request -- urlopen doesn't
     pool connections by default, so the header just makes HTTP/1.1
     semantics match the underlying behavior and stops any keepalive-recycle
     races on the server side.
  3. Transient transport errors (RemoteDisconnected / ConnectionReset / EOF
     before headers) trigger a bounded retry with small backoff.  HTTPError
     responses (4xx/5xx) are NOT retried -- those are real server answers
     callers route on by status code.

These tests pin the new behavior so it can't silently regress -- if a
future edit drops the cache, the header, or the retry loop, exactly one
test fails per missing piece with a clear message.
"""

import http.client
import ssl
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import pytest

import ravendb_client as rc


# Reset module-level state between tests so context caching from one test
# can't bleed into the next.
@pytest.fixture(autouse=True)
def _reset_ssl_cache():
    rc._SSL_CONTEXT_CACHE.clear()
    yield
    rc._SSL_CONTEXT_CACHE.clear()


# Real ssl.create_default_context tries to read the cafile off disk -- and
# every test here passes a fake path like "/ca.pem".  Stub it everywhere so
# the SSL setup is a no-op.  The ONE test that actually asserts the cache
# behavior installs its own counting wrapper on top.
@pytest.fixture(autouse=True)
def _stub_ssl_setup(monkeypatch):
    monkeypatch.setattr(
        rc.ssl, "create_default_context",
        lambda *a, **kw: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)


def _fake_response(status=200, body=b'{"ok":true}'):
    """Minimal stand-in for what urlopen returns under `with ... as response`."""
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


# ---- SSL context caching --------------------------------------------------

def test_ssl_context_is_built_once_per_cert_pair_and_reused(monkeypatch):
    """Old code called ssl.create_default_context + load_cert_chain on every
    request.  New code builds the context once and pulls it from the cache
    on subsequent requests for the same (ca, client_cert)."""
    # The autouse _stub_ssl_setup fixture already replaced create_default_context
    # with a no-disk stub.  Wrap THAT to count calls -- not the real one, which
    # would try to load /ca.pem off disk.
    create_calls = {"n": 0}
    stubbed_create = rc.ssl.create_default_context

    def counting_create(*a, **kw):
        create_calls["n"] += 1
        return stubbed_create(*a, **kw)
    monkeypatch.setattr(rc.ssl, "create_default_context", counting_create)

    # Three calls: pair-A used twice, pair-B used once.
    with patch.object(rc, "urlopen", return_value=_fake_response()) as mocked:
        rc.request("GET", "1a", "test.local", "/x", "/ca.pem", "/client-A.pem")
        rc.request("GET", "1a", "test.local", "/y", "/ca.pem", "/client-A.pem")
        rc.request("GET", "1a", "test.local", "/z", "/ca.pem", "/client-B.pem")

    print(f"\n    expected: 2 context builds (A reused on 2nd call, B fresh)")
    print(f"    actual:   create_default_context called {create_calls['n']} time(s)")
    print(f"              cache size: {len(rc._SSL_CONTEXT_CACHE)}\n")
    assert create_calls["n"] == 2
    assert mocked.call_count == 3
    assert len(rc._SSL_CONTEXT_CACHE) == 2


# ---- Connection: close header --------------------------------------------

def test_every_request_carries_connection_close_header(monkeypatch):
    """Defensive HTTP/1.1 semantics: tell the server we don't intend to keep
    the socket.  Stops any per-keepalive-connection limit on the server side
    from biting us mid-stream."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    captured = {}

    def capturing_urlopen(req, **kw):
        captured["headers"] = dict(req.header_items())
        return _fake_response()
    monkeypatch.setattr(rc, "urlopen", capturing_urlopen)

    rc.request("PUT", "1a", "test.local", "/path", "/ca.pem", "/c.pem",
               body={"k": "v"})

    print(f"\n    expected: 'Connection: close' on the outgoing request")
    print(f"    actual:   headers={captured['headers']}\n")
    # urllib title-cases header names, so check the title-cased form.
    assert captured["headers"].get("Connection") == "close"
    assert captured["headers"].get("Content-type") == "application/json"


# ---- transient-transport retry -------------------------------------------

def test_retries_on_remote_disconnected_and_succeeds(monkeypatch):
    """The motivating failure mode: server closes TCP between accept and
    sending the HTTP response.  urllib raises RemoteDisconnected.  A single
    retry should succeed if it was a transient close."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def flaky_urlopen(req, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise http.client.RemoteDisconnected(
                "Remote end closed connection without response")
        return _fake_response(status=201, body=b"ok")
    monkeypatch.setattr(rc, "urlopen", flaky_urlopen)
    # Skip the actual sleep so the test runs fast.
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    status, body = rc.request("PUT", "1a", "test.local", "/x",
                              "/ca.pem", "/c.pem", body={"k": 1})

    print(f"\n    expected: 2 attempts (1st RemoteDisconnected, 2nd 201); status=201")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 2
    assert status == 201
    assert body == b"ok"


def test_retries_on_connection_reset_wrapped_in_urlerror(monkeypatch):
    """urllib sometimes wraps the transport failure in URLError; the kind
    must still detect it as transient by unwrapping `.reason`."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def flaky_urlopen(req, **kw):
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise URLError(ConnectionResetError("Connection reset by peer"))
        return _fake_response(status=200, body=b"")
    monkeypatch.setattr(rc, "urlopen", flaky_urlopen)
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    status, _ = rc.request("GET", "1a", "test.local", "/x",
                           "/ca.pem", "/c.pem")

    print(f"\n    expected: 3 attempts (2 resets then ok); status=200")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 3
    assert status == 200


def test_exhausts_retries_then_raises(monkeypatch):
    """If transient failures keep happening past the retry budget, the kind
    raises the LAST exception so the caller sees the real failure (rather
    than a misleading None return or silent loop)."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def always_resets(req, **kw):
        attempts["n"] += 1
        raise http.client.RemoteDisconnected("not coming back")
    monkeypatch.setattr(rc, "urlopen", always_resets)
    monkeypatch.setattr(rc.time, "sleep", lambda _s: None)

    print(f"\n    expected: raises RemoteDisconnected after {rc._TRANSPORT_RETRY_MAX + 1} attempts")
    with pytest.raises(http.client.RemoteDisconnected, match="not coming back"):
        rc.request("PUT", "1a", "test.local", "/x", "/ca.pem", "/c.pem",
                   body=b"payload")
    print(f"    actual:   attempts={attempts['n']}\n")
    assert attempts["n"] == rc._TRANSPORT_RETRY_MAX + 1   # initial + retries


def test_does_not_retry_on_real_http_error(monkeypatch):
    """4xx / 5xx responses are real server answers.  Returning them once
    -- with the status code -- is the contract callers expect.  Retrying a
    500 would create user-visible duplicate-side-effect bugs."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def always_500(req, **kw):
        attempts["n"] += 1
        raise HTTPError(req.full_url, 500, "Internal Server Error",
                        hdrs=None, fp=None)
    # The HTTPError above lacks a body stream; patch .read() since the
    # production code does `return e.code, e.read()`.  raising=False lets
    # us add a method that doesn't pre-exist on the class.
    monkeypatch.setattr(HTTPError, "read",
                        lambda self: b"server boom", raising=False)
    monkeypatch.setattr(rc, "urlopen", always_500)

    status, body = rc.request("PUT", "1a", "test.local", "/x",
                              "/ca.pem", "/c.pem", body={"k": 1})

    print(f"\n    expected: 1 attempt only (HTTPError not retried); status=500")
    print(f"    actual:   attempts={attempts['n']}  status={status}\n")
    assert attempts["n"] == 1
    assert status == 500
    assert body == b"server boom"


def test_does_not_retry_on_generic_value_error(monkeypatch):
    """Non-transport errors (programmer bugs, malformed args) must not be
    swallowed by the retry loop -- they need to surface immediately."""
    monkeypatch.setattr(rc.ssl.SSLContext, "load_cert_chain", lambda *a, **kw: None)

    attempts = {"n": 0}

    def boom(req, **kw):
        attempts["n"] += 1
        raise ValueError("not a transport problem")
    monkeypatch.setattr(rc, "urlopen", boom)

    print(f"\n    expected: ValueError raised on first try, no retries")
    with pytest.raises(ValueError, match="not a transport problem"):
        rc.request("PUT", "1a", "test.local", "/x", "/ca.pem", "/c.pem")
    print(f"    actual:   attempts={attempts['n']}\n")
    assert attempts["n"] == 1
