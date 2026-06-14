import http.client
import json
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


# Test-only escape hatch: if a target appears in this dict, its mapped URL is
# used VERBATIM instead of composing https://<target>.<domain>:443.  Production
# leaves this empty.  Integration tests (which spin embedded RavenDB servers on
# random ports) populate it like:
#     TARGET_URL_OVERRIDES["1a"] = "http://127.0.0.1:46181"
# When the override is http://, SSL setup is skipped (no cert needed).
TARGET_URL_OVERRIDES: dict = {}


# Cache one SSLContext per (ca_cert, client_cert) pair.  Recreating the context
# on every call -- which is what the old code did -- re-parses both PEM files
# from disk per request.  Under a 50k-doc seed that's 50k file reads + cert
# chain validations on the controller, enough to noticeably slow the run and
# (depending on Python version) churn allocations.  Once-and-cached is what
# every production HTTPS client does.
_SSL_CONTEXT_CACHE: dict = {}


def _ssl_context_for(client_cert, ca_cert):
    key = (ca_cert, client_cert)
    ctx = _SSL_CONTEXT_CACHE.get(key)
    if ctx is None:
        ctx = ssl.create_default_context(cafile=ca_cert)
        ctx.load_cert_chain(certfile=client_cert)
        _SSL_CONTEXT_CACHE[key] = ctx
    return ctx


# How many times to retry a request that died mid-flight with a transport
# error.  RavenDB occasionally closes the TCP connection before sending the
# HTTP response (load shed, idle keepalive recycle, race between accept and
# the per-connection limiter).  urllib surfaces this as one of:
#   * http.client.RemoteDisconnected("Remote end closed connection without response")
#   * ConnectionResetError
#   * URLError wrapping either of the above
# Real HTTPError responses (4xx/5xx) are NOT retried -- those are real server
# answers, not transport failures.
_TRANSPORT_RETRY_MAX = 2
_TRANSPORT_RETRY_BACKOFF_SECS = (0.2, 0.5)   # one entry per retry attempt


def _is_transient_transport_error(exc):
    """Return True if `exc` is the kind of transport-level failure that's worth
    a quick retry (server closed mid-stream, connection reset, refused-accept,
    EOF before headers, broken-pipe on send).  HTTPError-style 4xx/5xx are
    explicitly NOT considered transient here -- those are real responses,
    callers handle them on the status code."""
    # Direct transport-failure exceptions urllib may surface (some Python
    # versions wrap them in URLError, others bubble them up directly).
    direct = (
        http.client.RemoteDisconnected,
        ConnectionResetError,
        ConnectionRefusedError,    # server's accept queue briefly full
        ConnectionAbortedError,    # server-side abort mid-stream
        BrokenPipeError,           # server closed while we were writing
        TimeoutError,              # socket-level read/write timeout
        EOFError,                  # peer closed before sending headers
        ssl.SSLEOFError,           # TLS terminated without close_notify
                                   # (Python's "UNEXPECTED_EOF_WHILE_READING")
    )
    if isinstance(exc, direct):
        return True
    if isinstance(exc, URLError):
        inner = getattr(exc, "reason", None)
        if isinstance(inner, direct):
            return True
        # Defensive: some Python versions surface TLS-EOF as a bare ssl.SSLError
        # (not the SSLEOFError subclass) with reason='UNEXPECTED_EOF_WHILE_READING'.
        # Match that specific reason -- but NOT all SSLErrors (cert-verify and
        # hostname-mismatch are real config bugs that mustn't be silently retried).
        if isinstance(inner, ssl.SSLError):
            reason = getattr(inner, "reason", "") or ""
            if "UNEXPECTED_EOF" in reason:
                return True
    return False


def request(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
    override = TARGET_URL_OVERRIDES.get(target)
    if override:
        url = override.rstrip("/") + path
        ctx = None
        if url.startswith("https://"):
            ctx = _ssl_context_for(client_cert, ca_cert)
    else:
        url = "https://" + target + "." + domain + ":443" + path
        ctx = _ssl_context_for(client_cert, ca_cert)

    data = None
    # `Connection: close` tells the server "don't keep this socket open after
    # the response".  urllib.urlopen doesn't pool by default, so we open a
    # fresh socket per call anyway -- this header just makes the HTTP/1.1
    # semantics match the underlying behavior and stops any keepalive-recycle
    # races on the server side.
    headers = {"Connection": "close"}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        headers["Content-Type"] = content_type or "application/json"
    elif isinstance(body, str):
        data = body.encode()
        headers["Content-Type"] = content_type or "application/octet-stream"
    elif isinstance(body, bytes):
        data = body
        headers["Content-Type"] = content_type or "application/octet-stream"

    req = Request(url, data=data, method=method, headers=headers)

    last_exc = None
    for attempt in range(_TRANSPORT_RETRY_MAX + 1):
        try:
            if ctx is not None:
                with urlopen(req, context=ctx, timeout=timeout) as response:
                    return response.status, response.read()
            else:
                with urlopen(req, timeout=timeout) as response:
                    return response.status, response.read()
        except HTTPError as e:
            # Real HTTP response from the server.  Not a transport failure;
            # callers expect (status_code, body) here and route on the code.
            return e.code, e.read()
        except Exception as e:
            if attempt < _TRANSPORT_RETRY_MAX and _is_transient_transport_error(e):
                time.sleep(_TRANSPORT_RETRY_BACKOFF_SECS[attempt])
                last_exc = e
                continue
            raise

    # Defensive: shouldn't reach here -- the loop returns or raises.  If it
    # does, surface the last seen exception rather than silently returning.
    raise last_exc if last_exc is not None else RuntimeError(
        "request() exhausted retries without an exception -- this is a bug")


def request_per_node(method, targets, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
    def call_one(target):
        try:
            status, response = request(method, target, domain, path,
                                       client_cert, ca_cert,
                                       body=body,
                                       content_type=content_type,
                                       timeout=timeout)
            return (target, status, response)
        except Exception as e:
            return (target, None, repr(e))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
        for result in pool.map(call_one, targets):
            results.append(result)
    return results
