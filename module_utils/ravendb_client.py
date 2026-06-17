import errno
import http.client
import json
import socket
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


# ---------------------------------------------------------------------------
# SSL context cache: build one per (ca_cert, client_cert) pair and reuse.
# Recreating the context on every call re-parses both PEM files from disk per
# request -- under a 50k-doc seed that's 50k file reads + cert chain
# validations on the controller, enough to noticeably slow the run and (on
# some Python builds) churn allocations / leak fds.
# ---------------------------------------------------------------------------
_SSL_CONTEXT_CACHE: dict = {}


def _ssl_context_for(client_cert, ca_cert):
    key = (ca_cert, client_cert)
    ctx = _SSL_CONTEXT_CACHE.get(key)
    if ctx is None:
        ctx = ssl.create_default_context(cafile=ca_cert)
        ctx.load_cert_chain(certfile=client_cert)
        _SSL_CONTEXT_CACHE[key] = ctx
    return ctx


# ---------------------------------------------------------------------------
# Retry on transient transport errors.
#
# Under sustained load against the RavenDB chaos lab we've seen every one of
# these flavors of transport failure at one point or another:
#   - TLS EOF without close_notify  (ssl.SSLEOFError, also as bare SSLError
#     with reason="UNEXPECTED_EOF_WHILE_READING")
#   - Server briefly closes the half-open keepalive socket
#     (http.client.RemoteDisconnected)
#   - Server's accept queue full briefly      (ConnectionRefusedError)
#   - Server-side abort mid-stream            (ConnectionAbortedError)
#   - Server reset existing socket            (ConnectionResetError)
#   - Server closed while we were sending     (BrokenPipeError, EPIPE)
#   - Socket-level read/write timeout         (TimeoutError, also socket.timeout)
#   - DNS resolver briefly unhappy            (socket.gaierror -- systemd-resolved
#     hiccups under burst)
#   - Truncated body / bad status line during cluster operation
#     (http.client.IncompleteRead, BadStatusLine, LineTooLong)
#   - Any of the above wrapped in URLError
#   - OSError carrying transient errno values (EAGAIN, EHOSTUNREACH, etc.)
#
# What we deliberately do NOT retry:
#   - HTTPError 4xx/5xx  -> real server responses, caller routes on the code
#   - ssl.SSLCertVerificationError / HOSTNAME_MISMATCH / CERTIFICATE_VERIFY_FAILED
#     -> real config bugs, retrying just delays the user-visible failure
#   - ValueError / TypeError -> programmer bugs in the caller
#
# Retry budget: 4 retries (5 tries total) with escalating backoff totaling
# ~3.7s.  Sized so RavenDB's post-config-change settle window (sub-second
# typically, 1-2s seen on kaiju) fits comfortably.  Real persistent failures
# still surface within ~4s.
# ---------------------------------------------------------------------------
_TRANSPORT_RETRY_MAX = 4
_TRANSPORT_RETRY_BACKOFF_SECS = (0.2, 0.5, 1.0, 2.0)

# ssl.SSLError.reason strings we treat as transient.  Everything else (cert
# verify, hostname mismatch, alert codes for client misconfig) bubbles up.
_TRANSIENT_SSL_REASONS = frozenset([
    "UNEXPECTED_EOF_WHILE_READING",
    "WRONG_VERSION_NUMBER",
    "SSL_ERROR_SYSCALL",
    "APPLICATION_DATA_AFTER_CLOSE_NOTIFY",
    "TLSV1_ALERT_INTERNAL_ERROR",
    "TLSV1_ALERT_USER_CANCELLED",
])

# OSError.errno values worth retrying.  Linux numbers; Windows uses the same
# names via the errno module so this works cross-platform.
_TRANSIENT_ERRNOS = frozenset([
    errno.ECONNREFUSED,    # 111  server briefly not accepting
    errno.ECONNRESET,      # 104  RST received
    errno.ECONNABORTED,    # 103  local abort
    errno.EPIPE,           # 32   broken pipe
    errno.ETIMEDOUT,       # 110  socket op timed out
    errno.EHOSTUNREACH,    # 113  routing blip
    errno.ENETUNREACH,     # 101  routing blip
    errno.ENETRESET,       # 102  network dropped during connection
    errno.EAGAIN,          # 11   resource temporarily unavailable
])
_eho = getattr(errno, "EHOSTDOWN", None)
if _eho is not None:
    _TRANSIENT_ERRNOS = _TRANSIENT_ERRNOS | {_eho}


# Direct transport-failure exception classes (some Python versions wrap them
# in URLError, others bubble them up directly).
_TRANSIENT_DIRECT_TYPES = (
    http.client.RemoteDisconnected,    # server closed before sending response
    http.client.BadStatusLine,         # garbage status line during restart
    http.client.IncompleteRead,        # body truncated mid-stream
    http.client.LineTooLong,           # response header line malformed under load
    ConnectionResetError,
    ConnectionRefusedError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,                      # PEP 3151 alias of socket.timeout
    socket.timeout,                    # explicit -- old Python paths still raise this
    socket.gaierror,                   # transient DNS / resolver hiccup
    EOFError,
    ssl.SSLEOFError,                   # TLS terminated without close_notify
)


def _is_transient_transport_error(exc):
    """Return True if `exc` is the kind of transport-level failure that's worth
    a quick retry.  Comprehensive: covers socket-level, HTTP-pre-response,
    TLS, and DNS classes that have surfaced under sustained chaos-lab load.

    HTTPError-style 4xx/5xx are explicitly NOT considered transient here --
    those are real responses, callers handle them on the status code.  Real
    SSL configuration errors (cert verification, hostname mismatch, etc.)
    are NOT retried either -- they'd never succeed on a retry."""
    if isinstance(exc, _TRANSIENT_DIRECT_TYPES):
        return True

    # ssl.SSLError without being SSLEOFError -- match on the reason string for
    # the specific transient cases we've seen.  Cert-verify failures keep
    # bubbling up.
    if isinstance(exc, ssl.SSLError):
        reason = getattr(exc, "reason", "") or ""
        if reason in _TRANSIENT_SSL_REASONS:
            return True

    # Bare OSError carrying a transient errno (covers OSError subclasses we
    # didn't enumerate above and the catch-all `OSError(EHOSTUNREACH, ...)`).
    if isinstance(exc, OSError) and not isinstance(exc, _TRANSIENT_DIRECT_TYPES):
        if getattr(exc, "errno", None) in _TRANSIENT_ERRNOS:
            return True

    # URLError commonly wraps every one of the above.  Unwrap one level
    # (recursively, in case of nested URLError -> URLError -> SSLError).
    if isinstance(exc, URLError):
        inner = getattr(exc, "reason", None)
        if inner is not None and _is_transient_transport_error(inner):
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
    # `Connection: close` -- urllib.urlopen doesn't pool connections by default,
    # so this just makes HTTP/1.1 semantics match the underlying one-shot
    # behavior and prevents any server-side per-keepalive limit from racing
    # with our request stream.
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
            # callers expect (status_code, body) and route on the code.
            return e.code, e.read()
        except Exception as e:
            if attempt < _TRANSPORT_RETRY_MAX and _is_transient_transport_error(e):
                time.sleep(_TRANSPORT_RETRY_BACKOFF_SECS[attempt])
                last_exc = e
                continue
            raise

    # Defensive: shouldn't reach here -- the loop returns or raises above.
    # If it does, surface the last seen exception rather than silently
    # returning, so the failure mode is visible.
    raise last_exc if last_exc is not None else RuntimeError(
        "request() exhausted retries without an exception -- this is a bug")


def stream_all_doc_ids(target, domain, db, client_cert, ca_cert,
                       page_size=4096, timeout=30):
    """Enumerate every live doc id on target/db.  Paginates /databases/<db>/docs
    until the server stops returning rows.  Returns a list in server order."""
    ids = []
    start = 0
    while True:
        path = ("/databases/%s/docs?start=%d&pageSize=%d&metadataOnly=true"
                % (db, start, page_size))
        status, body = request("GET", target, domain, path,
                               client_cert, ca_cert, timeout=timeout)
        if status != 200:
            raise RuntimeError("list docs on %s/%s start=%d failed: HTTP %d"
                               % (target, db, start, status))
        results = json.loads(body).get("Results") or []
        if not results:
            break
        for r in results:
            md = r.get("@metadata") or {}
            i = md.get("@id")
            if i is not None:
                ids.append(i)
        if len(results) < page_size:
            break
        start += page_size
    return ids


# ---------------------------------------------------------------------------
# Sharded DB routing.
#
# Rule (verified against the cursor 7.2 build and the official client, see
# Raven.Client/Documents/Operations/GetStatisticsOperation.cs and the handler
# attributes in Raven.Server/Documents/Sharding/Handlers/):
#
#   For every read against a sharded DB, send the call to the ORCHESTRATOR
#   node.  The orchestrator either:
#     - fan-outs and aggregates (e.g. /stats/essential, /streams/docs,
#       /docs paginated, /replication/conflicts)
#     - routes by id / by changeVector (e.g. /docs?id=X, /revisions?cv=...)
#     - proxies to a specific node when caller supplies ?nodeTag=<X>
#       (e.g. /stats, /stats/detailed, /replication/active-connections)
#
# Addressing shards directly via /databases/<db>$N/... is INTERNAL storage
# addressing; clients do not use it and the cursor build refuses some of
# those URLs with HTTP 410 DatabaseNotRelevant from non-owning nodes.
# ---------------------------------------------------------------------------


def orchestrator_for(target, domain, db, client_cert, ca_cert, timeout=30):
    """Return the orchestrator's container name for `db` as seen from `target`,
    or `target` unchanged if `db` is not sharded.

    Looks up DatabaseRecord.Sharding.Orchestrator.Topology.Members and maps
    the first member tag -> container name using the convention
    container = <cluster_prefix><tag.lower()>, where cluster_prefix is
    target[:-1] (e.g. '62a' -> '62', 'A' tag -> '62a').

    Returns the container name to address.  Raises RuntimeError on a
    non-200 admin/databases response so callers fail loud rather than
    silently calling the wrong node."""
    s, b = request("GET", target, domain,
                   "/admin/databases?name=%s" % db,
                   client_cert, ca_cert, timeout=timeout)
    if s != 200:
        raise RuntimeError(
            "orchestrator_for: %s/%s -- /admin/databases returned HTTP %d, "
            "can't determine routing" % (target, db, s))
    rec = json.loads(b)
    orch = (rec.get("Sharding") or {}).get("Orchestrator") or {}
    members = (orch.get("Topology") or {}).get("Members") or []
    if not members:
        # Not sharded; orchestrator concept doesn't apply.  The caller can
        # talk to `target` directly for any non-sharded endpoint.
        return target
    cluster_prefix = target[:-1]
    return "%s%s" % (cluster_prefix, members[0].lower())


def with_node_tag(path, tag):
    """Append ?nodeTag=<tag> (or &nodeTag=<tag> if a query already exists) to
    `path`.  Used for endpoints the orchestrator proxies per-node (/stats,
    /stats/detailed, /replication/active-connections).  Pass the cluster
    tag of the target node (e.g. 'A' for container '<prefix>a')."""
    sep = "&" if "?" in path else "?"
    return "%s%snodeTag=%s" % (path, sep, tag)


def prefix_match(doc_id, prefixes):
    """True if doc_id is covered by any of the given prefix patterns.
    Accepts trailing '*' or '/' on a prefix; both mean 'starts with stem/'."""
    if not prefixes:
        return False
    for raw in prefixes:
        stem = raw.rstrip("*").rstrip("/")
        if not stem:
            continue
        if doc_id == stem or doc_id.startswith(stem + "/"):
            return True
    return False


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


def resolve_db_admin_route(target, db_name, domain, client_cert, ca_cert):
    """Find the node to send database-record-level admin calls to (revisions
    config, replication-task config, anything that writes the DatabaseRecord).

    For non-sharded databases: return `target` unchanged -- any cluster member
    holding the database accepts these calls.

    For sharded databases: return the container name of the database's first
    orchestrator member.  Non-orchestrator members of a sharded database
    reject admin/<db>/ calls with HTTP 410
    (Raven.Client.Exceptions.Database.DatabaseNotRelevantException -- "Can't
    get or add orchestrator for database X because it is not relevant on this
    node Y").  RavenDB does NOT auto-forward these to the orchestrator the way
    it forwards per-doc reads, so the client has to route explicitly.

    Returns the node name to use as `target` for the subsequent call.  Raises
    RuntimeError if /admin/databases?name= is unreachable or if the db is
    sharded but has no orchestrator (which would indicate a topology bug)."""
    try:
        s, b = request("GET", target, domain,
                       "/admin/databases?name=%s" % db_name,
                       client_cert, ca_cert)
    except Exception:
        # Transport-level failure (DNS / connection refused / timeout) --
        # we can't determine routing, so surface this loudly rather than
        # silently sending to the (potentially-wrong) original target.
        raise RuntimeError(
            "%s/%s: /admin/databases unreachable -- can't determine sharded routing"
            % (target, db_name))
    if s != 200:
        raise RuntimeError(
            "%s/%s: /admin/databases returned HTTP %d -- db missing or node unreachable"
            % (target, db_name, s))

    rec = json.loads(b)
    if not (rec.get("Sharding") or {}).get("Shards"):
        # Non-sharded -- the target accepts admin calls directly.
        return target

    orch_members = (((rec.get("Sharding") or {}).get("Orchestrator") or {})
                    .get("Topology") or {}).get("Members") or []
    if not orch_members:
        raise RuntimeError(
            "%s/%s: sharded db has no orchestrator members -- can't route admin call"
            % (target, db_name))

    # `target` looks like "62a"; the cluster id is everything but the last
    # character (so "62"), and the orchestrator member tag (e.g. "G") becomes
    # the lowercase suffix -> "62g".
    cluster_id = target[:-1]
    return "%s%s" % (cluster_id, orch_members[0].lower())
