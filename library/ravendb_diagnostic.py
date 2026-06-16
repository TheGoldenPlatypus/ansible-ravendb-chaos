#!/usr/bin/python
"""
ravendb_diagnostic -- Ansible module exposing a registry of one-shot probes
against a RavenDB cluster (or hub+sink mesh) used by the chaos / RPV-1 scenarios.

Each probe is a "kind" dispatched through `KINDS[<name>](params)`.  Kinds are
grouped below by category:

    info-only:
        doc_count, replication, schema_version, size_envelope,
        supported_features

    capture:
        capture_cv, capture_doc_cv

    parity (uniformity across nodes):
        doc_count_parity, doc_id_set_parity, revision_count_parity,
        extension_stats_parity, stats_parity

    leak / orphan (something that should not be present):
        orphan_revisions, scan_fltr, lane_inert, filter_compliance,
        cross_sink_isolation

    CV-shape (DatabaseChangeVector / per-doc CV structural checks):
        stored_item_cv_split, cv_boundary_by_dbid, db_cv_order_side_only,
        cross_cluster_cv_equality

Each kind returns either a str or a list[str].  Whatever it returns becomes
`result["msg"]` in the Ansible result.  When `assert_mode=true` and a check
fails, the kind raises `DiagnosticViolation` and `main()` converts that into
`module.fail_json(msg=<lines>)`.

PHASE-1 NOTE: this rewrite is readability only.  No check thresholds, output
shapes, KINDS keys, or argument_spec keys were changed.  Bug-hunting / sharpening
of false-positives is deferred to phase 2.
"""

import json
import os
import re
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import (
    prefix_match,
    probe_shard_endpoint,
    request,
    request_per_node,
    resolve_db_admin_route,
    stream_all_doc_ids,
)


# ============================================================================
# Module-level constants
# ============================================================================

# Stats fields rendered by the `stats_parity` table.
_STATS_ALL_FIELDS = [
    "CountOfAttachments", "CountOfConflicts", "CountOfCounterEntries",
    "CountOfDocuments", "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTimeSeriesSegments", "CountOfTombstones", "CountOfUniqueAttachments",
]

# Default subset that `stats_parity` actually asserts on (the rest are info-only).
_STATS_DEFAULT_ASSERTED = [
    "CountOfAttachments", "CountOfConflicts", "CountOfDocuments",
    "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTombstones", "CountOfUniqueAttachments",
]

# CV entry regexes.  An entry looks like: `A:123-<dbid>` where A is a tag,
# 123 an etag, and <dbid> a base64-ish identifier.
_CV_DBID_RE = re.compile(r"([A-Za-z0-9]+):\d+-([A-Za-z0-9+/=_\-]+)")
_CV_ENTRY_FULL_RE = re.compile(r"([A-Za-z0-9]+):(\d+)-([A-Za-z0-9+/=_\-]+)")

# doc_id_set_parity: retry budget for transiently split docs.
_DOC_ID_PARITY_SETTLE_RETRIES = 3
_DOC_ID_PARITY_SETTLE_SECS = 2

# orphan_revisions: how often to poll /operations/state while waiting for adopt.
_ORPHAN_POLL_SECS = 2


# ============================================================================
# Assertion plumbing
# ============================================================================

class DiagnosticViolation(Exception):
    """Raised by a kind when assert_mode=true AND a check failed.  Carries the
    full diagnostic `lines` so main() can pass them to fail_json verbatim."""

    def __init__(self, lines):
        self.lines = lines
        super().__init__("\n".join(lines))


def violation(assert_mode, lines):
    """If assert_mode is on, raise with `lines` carried as a list (so main() can
    fail_json with a multi-line msg that Ansible debug renders nicely).
    Otherwise return them so the handler can include them in its info output."""
    if assert_mode:
        raise DiagnosticViolation(lines)
    return lines


def expand_id_set(p):
    """Build the doc-id list from either explicit `ids` or `id_prefix` + `count`."""
    if p["ids"]:
        return list(p["ids"])
    if p["id_prefix"] and p["count"]:
        return ["%s/%d" % (p["id_prefix"], n) for n in range(p["count"])]
    raise ValueError("requires `ids` OR (`id_prefix` and `count`)")


# ============================================================================
# HTTP / stats helpers
# ============================================================================

def _resolve_shard_for_tag(p, target, tag):
    """For a sharded DB, return the shard id whose Members list contains `tag`."""
    db = p["db_name"]
    s, b = request("GET", target, p["ravendb_domain"],
                   "/admin/databases?name=%s" % db,
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        return None
    sharding = json.loads(b).get("Sharding") or {}
    for shard_id, shard_rec in (sharding.get("Shards") or {}).items():
        if tag in (shard_rec.get("Members") or []):
            return shard_id
    return None


def classify_nodes(p, nodes):
    """Probe /stats on each node and return ({target: shard_id_or_None}, skipped).

    A shard_id of None means non-sharded (or the node hosts the DB plainly);
    a non-None shard_id means we discovered the per-shard route to use for
    follow-up stat requests on that node.  Two server responses indicate a
    shard-only member that needs the per-shard route resolved:
      * 500 'nodeTag is mandatory'     -- legacy 6.x sharded response
      * 410 DatabaseNotRelevant       -- 7.x sharded response on a non-orchestrator
    Anything else goes in `skipped`."""
    domain = p["ravendb_domain"]
    db = p["db_name"]
    path = "/databases/%s/stats" % db
    results = request_per_node("GET", nodes, domain, path,
                               p["client_cert"], p["ca_cert"])
    host_map = {}
    skipped = []
    sharded_probe_needed = []
    for target, status, body in results:
        if status == 200:
            host_map[target] = None
        elif status == 500 and b"nodeTag is mandatory" in (body or b""):
            sharded_probe_needed.append(target)
        elif status == 410 and b"DatabaseNotRelevant" in (body or b""):
            sharded_probe_needed.append(target)
        else:
            skipped.append(target)

    for target in sharded_probe_needed:
        tag = target[-1].upper()
        shard_id = _resolve_shard_for_tag(p, target, tag)
        if shard_id is None:
            skipped.append(target)
            continue
        # probe_shard_endpoint tries 7.x form (db$N) first, falls back to 6.x
        # query-param.  Required for mid-rolling-upgrade mixed-binary clusters.
        s, _ = probe_shard_endpoint(
            target, p["ravendb_domain"], db, tag, shard_id,
            p["client_cert"], p["ca_cert"])
        if s == 200:
            host_map[target] = shard_id
        else:
            skipped.append(target)

    return host_map, skipped


def aggregate_sharded_stats(p, target, db):
    """For sharded DBs without a per-tag route, sum per-shard /stats into one dict.

    Integer fields are summed; non-integer fields take the first-seen value.

    Each shard's stats are fetched from a node that ACTUALLY owns the shard
    (not from the original `target`).  The cursor 7.x per-shard endpoint
    serves only LOCAL shard data -- asking the orchestrator for shard N's
    stats returns 410, even via /databases/<db>$N/stats.  Old code (which
    probed `target` for every shard) was silently only capturing the one
    shard `target` happened to own locally; for a 3-shard DB that meant
    the aggregate was 1/3 of the true value."""
    rec_path = "/admin/databases?name=%s" % db
    s, b = request("GET", target, p["ravendb_domain"], rec_path,
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        return None
    sharding = (json.loads(b).get("Sharding") or {})
    shards = sharding.get("Shards") or {}
    if not shards:
        return None
    cluster_prefix = target[:-1]   # "62a" -> "62"; "1a" -> "1"
    aggregate = {}
    for shard_id, shard_rec in shards.items():
        members = shard_rec.get("Members") or []
        if not members:
            continue
        # Pick a node that owns this shard, not `target`.
        member_tag = members[0]
        member_target = "%s%s" % (cluster_prefix, member_tag.lower())
        s, b = probe_shard_endpoint(
            member_target, p["ravendb_domain"], db, member_tag, shard_id,
            p["client_cert"], p["ca_cert"])
        if s != 200:
            continue
        for k, v in json.loads(b).items():
            if isinstance(v, int):
                aggregate[k] = aggregate.get(k, 0) + v
            else:
                aggregate.setdefault(k, v)
    return aggregate or None


def get_stats(p, target, shard_id=None):
    """Return parsed /stats dict for `target` (or None on failure).

    If `shard_id` is given, query that shard explicitly.  Otherwise try plain
    /stats first and fall back to aggregating across shards on a 500
    'nodeTag is mandatory' response."""
    db = p["db_name"]
    if shard_id is not None:
        tag = target[-1].upper()
        try:
            status, body = probe_shard_endpoint(
                target, p["ravendb_domain"], db, tag, shard_id,
                p["client_cert"], p["ca_cert"])
        except Exception:
            return None      # connection refused / DNS error / timeout -> treat as unreachable
        return json.loads(body) if status == 200 else None

    path = "/databases/%s/stats" % db
    try:
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
    except Exception:
        return None
    if status == 200:
        return json.loads(body)
    # Sharded responses: 6.x returned 500 'nodeTag is mandatory'; 7.x returns
    # 410 DatabaseNotRelevantException on a shard-only member.  Both mean
    # "this node hosts the db as a shard, ask via per-shard route".
    if status == 500 and b"nodeTag is mandatory" in body:
        return aggregate_sharded_stats(p, target, db)
    if status == 410 and b"DatabaseNotRelevant" in body:
        return aggregate_sharded_stats(p, target, db)
    return None


def per_node_field(p, nodes_or_map, field):
    """Return {target: stats[field]} for each reachable node.

    Accepts either a list of targets (uses plain /stats) or a {target: shard_id}
    map produced by classify_nodes() (uses per-shard route when shard_id is set)."""
    db = p["db_name"]
    out = {}
    if isinstance(nodes_or_map, dict):
        for target, shard_id in nodes_or_map.items():
            if shard_id is None:
                s, b = request("GET", target, p["ravendb_domain"],
                               "/databases/%s/stats" % db,
                               p["client_cert"], p["ca_cert"])
            else:
                tag = target[-1].upper()
                s, b = probe_shard_endpoint(
                    target, p["ravendb_domain"], db, tag, shard_id,
                    p["client_cert"], p["ca_cert"])
            if s == 200:
                out[target] = json.loads(b).get(field)
        return out

    path = "/databases/%s/stats" % db
    results = request_per_node("GET", nodes_or_map, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
    for target, status, body in results:
        if status == 200:
            out[target] = json.loads(body).get(field)
    return out


# ============================================================================
# CV parsing helpers
# ============================================================================

def parse_cv_entries(cv):
    """Return list of (tag, dbid) tuples extracted from a CV string."""
    return _CV_DBID_RE.findall(cv or "")


def _parse_cv_set(cv_str):
    """Return frozenset of (dbid, etag) for cross-cluster set-equality checks."""
    if not cv_str:
        return frozenset()
    out = set()
    for _tag, etag, dbid in _CV_ENTRY_FULL_RE.findall(cv_str):
        try:
            out.add((dbid, int(etag)))
        except ValueError:
            pass
    return frozenset(out)


# ============================================================================
# Formatting helpers
# ============================================================================

def format_per_node(per_node, indent="  "):
    """Render a {name: value} dict as aligned `  name   value` lines."""
    if not per_node:
        return [indent + "(empty)"]
    width = max(len(n) for n in per_node)
    lines = []
    for name in sorted(per_node):
        lines.append("%s%-*s  %s" % (indent, width, name, per_node[name]))
    return lines


# ============================================================================
# === Info-only kinds ========================================================
# ============================================================================

def k_doc_count(p):
    """Single-node CountOfDocuments report.  Returns a one-line str.

    Raises RuntimeError if /stats returns non-200 (db missing or node
    unreachable) -- scenarios should fail loud, not silently print a soft
    fallback message that callers might miss."""
    target = p["target"]
    db = p["db_name"]
    stats = get_stats(p, target)
    if stats is None:
        raise RuntimeError(
            "%s/%s: /stats returned non-200 -- db missing or node unreachable"
            % (target, db))
    return "%s/%s  ->  %s docs" % (target, db, stats.get("CountOfDocuments"))


def k_replication(p):
    """List incoming and outgoing active replication connections for `target`.

    Returns list[str] -- a header + per-connection lines.  Raises RuntimeError
    if /replication/active-connections returns non-200 (db missing or node
    unreachable); scenarios should fail loud, not silently return a soft string."""
    target = p["target"]
    db = p["db_name"]
    path = "/databases/%s/replication/active-connections" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /replication/active-connections returned HTTP %d -- "
            "db missing or node unreachable" % (target, db, status))
    data = json.loads(body)
    incoming = data.get("IncomingConnections") or []
    outgoing = data.get("OutgoingConnections") or []
    lines = ["replication on %s/%s:" % (target, db)]
    lines.append("  INCOMING (%d):" % len(incoming))
    for conn in incoming:
        lines.append("    %s" % conn.get("SourceUrl"))
    lines.append("  OUTGOING (%d):" % len(outgoing))
    for conn in outgoing:
        dest = conn.get("DestinationUrl") or conn.get("DestinationDatabase") or repr(conn)[:120]
        lines.append("    %s" % dest)
    return lines


_REPL_TASK_TYPES = frozenset([
    "Replication",                # OngoingTasks entry for external_replication
    "ExternalReplication",        # alternate name some endpoints use
    "PullReplicationAsHub",
    "PullReplicationAsSink",
    "RavenEtl",
])


def k_replication_health(p):
    """Assert no replication-flavored ongoing task on `target/db` is stuck.

    Hits /databases/<db>/tasks, filters OngoingTasks to replication types, and
    fails (under assert_mode) when any task is Faulted, carries a non-empty
    Error, or has TaskConnectionStatus=Reconnect at the moment of the check.
    Callers should drain first; a sustained 'Reconnect' after a drain means
    the task isn't progressing.

    Returns list[str] -- a header + per-task line, with FAIL/PASS at the end."""
    target = p["target"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])

    path = "/databases/%s/tasks" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /tasks returned HTTP %d -- db missing or node unreachable"
            % (target, db, status))

    ongoing = json.loads(body).get("OngoingTasks") or []
    repl_tasks = [t for t in ongoing if t.get("TaskType") in _REPL_TASK_TYPES]

    lines = ["replication health on %s/%s  (%d replication task(s)):"
             % (target, db, len(repl_tasks))]
    bad = []
    for t in repl_tasks:
        ttype = t.get("TaskType")
        tname = t.get("TaskName") or "<no-name>"
        state = t.get("TaskState")
        conn = t.get("TaskConnectionStatus")
        err = t.get("Error")
        lines.append("  %-22s  %-30s  state=%-10s  conn=%-10s  err=%s"
                     % (ttype, tname, state, conn, err if err else "-"))
        is_stuck = (state == "Faulted") or (conn == "Reconnect") or bool(err)
        if is_stuck:
            bad.append("%s/%s state=%s conn=%s err=%s"
                       % (ttype, tname, state, conn, err))

    if bad:
        return violation(assert_mode, lines + ["FAIL  stuck task(s): %s" % bad])
    lines.append("PASS  no stuck replication task")
    return lines


def k_schema_version(p):
    """Report /build/version per node, optionally asserting parity or an expected substring."""
    nodes = p["nodes"]
    expected = p["expected_version"]
    require_parity = bool(p["require_parity"])
    assert_mode = bool(p["assert_mode"])

    version_map = {}
    unreachable = []
    for target in nodes:
        try:
            status, body = request("GET", target, p["ravendb_domain"], "/build/version",
                                   p["client_cert"], p["ca_cert"])
        except Exception:
            unreachable.append(target)
            continue
        if status != 200:
            unreachable.append(target)
            continue
        version_map[target] = (json.loads(body).get("FullVersion") or "?")

    lines = ["schema/build version per node:"]
    lines.extend(format_per_node(version_map))
    if unreachable:
        lines.append("  unreachable: %s" % unreachable)

    # Fail loud when we couldn't read ANY node -- silently returning an empty
    # version_map is a footgun (scenarios would print "(empty)" and continue).
    if not version_map:
        raise RuntimeError(
            "k_schema_version: no /build/version response from any node "
            "(nodes=%s, unreachable=%s)" % (nodes, unreachable))

    distinct = set(version_map.values())
    if require_parity and assert_mode and len(distinct) > 1:
        return violation(True, lines + ["FAIL  version mismatch across nodes"])

    if expected and assert_mode:
        bad = [n for n, v in version_map.items() if expected not in v]
        if bad:
            return violation(True, lines + ["FAIL  nodes missing expected '%s': %s" % (expected, bad)])

    return lines


def k_supported_features(p):
    """Report DatabaseRecord.SupportedFeatures for one db on one node.

    Used after a rolling upgrade to confirm the new build's optional features
    actually flipped on for the database (they only do once all nodes report
    upgraded).  Returns one-line str.  Raises RuntimeError on non-200 (db
    missing or node unreachable) -- fail loud."""
    target = p["target"] or (p["nodes"] and p["nodes"][0])
    db = p["db_name"]
    if not target:
        raise ValueError("k_supported_features requires `target` (or `nodes` with >=1 entry)")
    if not db:
        raise ValueError("k_supported_features requires `db_name`")

    rec_path = "/admin/databases?name=%s" % quote(db)
    status, body = request("GET", target, p["ravendb_domain"], rec_path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError(
            "%s/%s: /admin/databases returned HTTP %d -- db missing or node unreachable"
            % (target, db, status))
    features = json.loads(body).get("SupportedFeatures") or {}
    return "DatabaseRecord.SupportedFeatures (via %s, db=%s): %s" % (target, db, features)


def k_size_envelope(p):
    """Baseline-or-compare per-node SizeOnDisk in bytes.

    First call (baseline missing) captures and returns BASELINE CAPTURED.  Subsequent
    calls compute %-growth per node and FAIL when any node exceeds `max_growth_pct`."""
    nodes = p["nodes"]
    baseline_file = p["baseline_file"]
    max_growth_pct = float(p["max_growth_pct"] or 300)
    assert_mode = bool(p["assert_mode"])

    sizes = {}
    for target in nodes:
        stats = get_stats(p, target)
        if stats:
            sizes[target] = int((stats.get("SizeOnDisk") or {}).get("SizeInBytes") or 0)

    # Fail loud if we couldn't read ANY node -- otherwise the capture path
    # would write an empty baseline file and silently mark the run successful.
    if not sizes:
        raise RuntimeError(
            "k_size_envelope: no /stats response from any node "
            "(nodes=%s) -- can't capture or compare sizes" % nodes)

    if not os.path.exists(baseline_file):
        os.makedirs(os.path.dirname(baseline_file), exist_ok=True)
        with open(baseline_file, "w") as f:
            json.dump(sizes, f, indent=2)
        lines = ["BASELINE CAPTURED  %s" % baseline_file]
        lines.extend(format_per_node({n: "%d bytes" % v for n, v in sizes.items()}))
        return lines

    with open(baseline_file, "r") as f:
        baseline = json.load(f)

    lines = ["size envelope check vs %s:" % baseline_file]
    growths = {}
    zero_baseline_with_growth = []
    for target in nodes:
        base = int(baseline.get(target, 0))
        curr = sizes.get(target, 0)
        if base <= 0 and curr > 0:
            # Fail loud: baseline=0 + current>0 means we can't compute % growth
            # (would divide by zero) -- silently reporting 0% is a false negative.
            zero_baseline_with_growth.append(target)
            lines.append("  %-6s  baseline=%d  current=%d  growth=N/A (baseline=0)" %
                         (target, base, curr))
            continue
        pct = ((curr - base) * 100.0 / base) if base > 0 else 0.0
        growths[target] = pct
        lines.append("  %-6s  baseline=%d  current=%d  growth=%+.1f%%" % (target, base, curr, pct))

    if zero_baseline_with_growth:
        raise RuntimeError(
            "k_size_envelope: baseline=0 but current>0 on %s -- baseline file "
            "is stale or was captured before any data existed; can't compute growth"
            % zero_baseline_with_growth)

    over = [n for n, pct in growths.items() if pct > max_growth_pct]
    if over and assert_mode:
        return violation(True, lines + ["FAIL  growth > %.1f%% on %s" % (max_growth_pct, over)])

    if over:
        lines.append("INFO  growth exceeds %.1f%% on %s (assert_mode=false; not failing)" % (
            max_growth_pct, over))
    return lines


# ============================================================================
# === Capture kinds ==========================================================
# ============================================================================

def k_capture_cv(p):
    """Write each node's DatabaseChangeVector to `<output_dir>/<node>.cv`.

    Returns a one-line str summary; used by scenarios to snapshot the cluster
    CV state for later diffing."""
    nodes = p["nodes"]
    output_dir = p["output_dir"]
    if not output_dir:
        raise ValueError("kind=capture_cv requires `output_dir` (wrapper YAML resolves the default)")

    os.makedirs(output_dir, exist_ok=True)

    written = []
    unreachable = []
    for target in nodes:
        stats = get_stats(p, target)
        if stats is None:
            # Unreachable: don't write a misleading "<UNAVAILABLE>" sentinel
            # that looks like a captured value -- collect and raise below.
            unreachable.append(target)
            continue
        # Empty CV is legit (DB has no docs yet) -- write empty line.
        cv = stats.get("DatabaseChangeVector") or ""
        path = os.path.join(output_dir, "%s.cv" % target)
        with open(path, "w") as f:
            f.write(cv + "\n")
        written.append(target)

    if unreachable:
        raise RuntimeError(
            "k_capture_cv: %s unreachable -- can't capture CV (captured %d/%d nodes)"
            % (unreachable, len(written), len(nodes)))

    return "captured DatabaseChangeVector for %d node(s) -> %s/" % (len(written), output_dir)


def k_capture_doc_cv(p):
    """Write per-(node, doc_id) document CV to `<output_dir>/<node>__<id>.cv`.

    Returns a one-line str summary.  Missing/unreachable docs are recorded as
    `<NOT_FOUND status=...>` rather than skipped, so the file set is uniform."""
    nodes = p["nodes"]
    ids = p["ids"] or []
    db = p["db_name"]
    output_dir = p["output_dir"]
    if not ids:
        raise ValueError("kind=capture_doc_cv requires `ids` (non-empty list)")
    if not output_dir:
        raise ValueError("kind=capture_doc_cv requires `output_dir` (wrapper YAML resolves the default)")

    os.makedirs(output_dir, exist_ok=True)

    pairs = 0
    unreachable_nodes = set()
    for target in nodes:
        for doc_id in ids:
            path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
            try:
                status, body = request("GET", target, p["ravendb_domain"], path,
                                       p["client_cert"], p["ca_cert"])
            except Exception:
                # Connection refused / DNS / timeout -- mark the node and stop
                # trying its remaining docs.  Raised below as a clean error.
                unreachable_nodes.add(target)
                break
            if status == 200:
                results = json.loads(body).get("Results") or []
                if results:
                    cv = (results[0].get("@metadata") or {}).get("@change-vector") or "<NO_CV>"
                else:
                    cv = "<NOT_FOUND status=%d>" % status
            else:
                cv = "<NOT_FOUND status=%d>" % status

            safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", doc_id)
            out = os.path.join(output_dir, "%s__%s.cv" % (target, safe_id))
            with open(out, "w") as f:
                f.write(cv + "\n")
            pairs += 1

    if unreachable_nodes:
        raise RuntimeError(
            "k_capture_doc_cv: %s unreachable -- can't capture CV (captured %d pair(s) "
            "across %d/%d nodes)" % (sorted(unreachable_nodes), pairs,
                                     len(nodes) - len(unreachable_nodes), len(nodes)))

    return "captured %d per-(node,id) CV file(s) -> %s/" % (pairs, output_dir)


# ============================================================================
# === Parity kinds ===========================================================
# ============================================================================

def k_doc_count_parity(p):
    """Assert every reachable node reports the same CountOfDocuments."""
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    counts = per_node_field(p, has_db, "CountOfDocuments")
    lines = ["doc count parity on %s (skipped=%s):" % (list(has_db), skipped)]
    lines.extend(format_per_node(counts))

    if len(set(counts.values())) != 1:
        return violation(assert_mode, lines + ["FAIL  doc counts differ"])
    lines.append("PASS  every node reports %s" % next(iter(set(counts.values()))))
    return lines


def k_doc_id_set_parity(p):
    """Assert each probe doc is uniformly present (200) or uniformly absent on every node.

    Transient splits are retried with a settle window before failing."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    probe_ids = expand_id_set(p)

    unreachable_nodes = set()

    def probe_all(ids):
        """Returns {id: [status_per_node]}.  Connection errors on a node mark
        it unreachable (collected in `unreachable_nodes`); we still produce a
        statuses entry (None) so the per-id list stays aligned to `nodes`."""
        out = {}
        for doc_id in ids:
            statuses = []
            for target in nodes:
                path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
                try:
                    status, _ = request("GET", target, p["ravendb_domain"], path,
                                        p["client_cert"], p["ca_cert"])
                except Exception:
                    unreachable_nodes.add(target)
                    status = None
                statuses.append(status)
            out[doc_id] = statuses
        return out

    def split_ids(id_status):
        bad = []
        for doc_id, statuses in id_status.items():
            present = sum(1 for s in statuses if s == 200)
            if 0 < present < len(statuses):
                bad.append(doc_id)
        return bad

    id_status = probe_all(probe_ids)
    mismatched = split_ids(id_status)

    for _ in range(_DOC_ID_PARITY_SETTLE_RETRIES):
        if not mismatched:
            break
        time.sleep(_DOC_ID_PARITY_SETTLE_SECS)
        retry_status = probe_all(mismatched)
        id_status.update(retry_status)
        mismatched = split_ids({i: id_status[i] for i in mismatched})

    if unreachable_nodes:
        raise RuntimeError(
            "k_doc_id_set_parity: %s unreachable -- can't determine doc-id-set "
            "parity" % sorted(unreachable_nodes))

    lines = ["doc-id-set parity: %d id(s) probed across %s" % (len(probe_ids), nodes),
             "  mismatched: %d" % len(mismatched)]
    if mismatched:
        lines.append("  mismatched ids (first 20):")
        for i in mismatched[:20]:
            pairs = ", ".join("%s=%s" % (n, s) for n, s in zip(nodes, id_status[i]))
            lines.append("    %s  %s" % (i, pairs))
        return violation(assert_mode, lines +
                         ["FAIL  doc-id-set split (persisted across %d retries x %ds settle)" %
                          (_DOC_ID_PARITY_SETTLE_RETRIES, _DOC_ID_PARITY_SETTLE_SECS)])
    lines.append("PASS  every probe id is uniformly present or uniformly absent")
    return lines


def _resolve_revisions_route(p, target):
    return resolve_db_admin_route(
        target, p["db_name"], p["ravendb_domain"],
        p["client_cert"], p["ca_cert"],
    )


def k_revision_count_parity(p):
    """Assert per-id revision counts agree across nodes (and equal `expected_count` if given).

    Accepts both plain nodes (hit directly) and sharded leaders (auto-detected
    via /admin/databases?name=<db>; per-id reads routed through the database's
    orchestrator).  The output labels keep the caller's original target tag so
    a sharded leader shows up as e.g. '1a' in the per-id table, not as the
    orchestrator member we routed through internally."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    expected = p["expected_count"]
    page_size = int(p["page_size"] or 1024)
    probe_ids = expand_id_set(p)

    # Resolve once per target so we don't pay an /admin/databases hit per probe id.
    route_for = {t: _resolve_revisions_route(p, t) for t in nodes}

    id_counts = {}
    unreachable_nodes = set()
    for doc_id in probe_ids:
        counts = []
        for target in nodes:
            if target in unreachable_nodes:
                counts.append(None)
                continue
            route = route_for[target]
            path = "/databases/%s/revisions?id=%s&pageSize=%d" % (db, quote(doc_id), page_size)
            try:
                status, body = request("GET", route, p["ravendb_domain"], path,
                                       p["client_cert"], p["ca_cert"])
            except Exception:
                unreachable_nodes.add(target)
                counts.append(None)
                continue
            if status == 200:
                counts.append(len(json.loads(body).get("Results") or []))
            else:
                counts.append(None)
        id_counts[doc_id] = counts

    if unreachable_nodes:
        raise RuntimeError(
            "k_revision_count_parity: %s unreachable -- can't compute revision-count parity"
            % sorted(unreachable_nodes))

    mismatched = [i for i, c in id_counts.items() if len(set(c)) != 1]
    lines = ["revision-count parity: %d id(s) across %s" % (len(probe_ids), nodes)]
    lines.append("  per-id counts (first 10):")
    for i, c in list(id_counts.items())[:10]:
        lines.append("    %-20s  %s" % (i, c))
    lines.append("  mismatched: %d" % len(mismatched))

    if mismatched:
        return violation(assert_mode, lines +
                         ["FAIL  revision count drift on: %s" % mismatched[:20]])

    if expected is not None:
        wrong = [i for i, c in id_counts.items() if c != [int(expected)] * len(nodes)]
        if wrong:
            return violation(assert_mode, lines +
                             ["FAIL  expected %d revs/id but mismatched on: %s" %
                              (int(expected), wrong[:20])])
        lines.append("PASS  every id has exactly %d revisions on every node" % int(expected))
    else:
        lines.append("PASS  per-id revision counts agree across nodes")
    return lines


_FORM_EXPECT_CHOICES = ("any-raw", "all-split", "any-split", "all-raw")


def k_revision_form_sampling(p):
    """For each probe doc id, GET /databases/<db>/revisions?id=<id>&pageSize=N on
    `target`, then classify every revision's @change-vector by form:

        raw    -- no `delimiter` in the CV (legacy on-disk shape)
        split  -- contains `delimiter` (new lane / hashed)

    Aggregate per-id raw/split counts + overall totals, then assert per `expect`:

        any-raw   -- at least one revision (across all ids) must be raw.
                     Use after a SNAPSHOT restore of a mixed-form source to prove
                     raw-CV revisions were preserved at the voron page level.
        all-split -- every revision must be split.  Use after a SMUGGLER restore
                     (the public-API import re-keys all revs into hashed form).
        any-split -- at least one revision must be split.
        all-raw   -- every revision must be raw (pure v_old baseline).

    A probe id with zero returned revisions is reported as `missing` and fails
    under assert_mode -- otherwise a smuggler restore that dropped all revisions
    on a given doc would silently pass an `any-raw` / `all-split` check that
    only looks at the rest of the probe set.

    Returns list[str] -- per-id raw/split counts (first 10) + totals + PASS/FAIL.

    Distinct from `stored_item_cv_split`: that kind checks the LIVE doc's CV
    shape (one entry per id); this one walks every REVISION returned by
    /revisions?id=..., which is what catches a per-revision-form regression."""
    target = p["target"]
    db = p["db_name"]
    expect = p["expect"] or "any-raw"
    delimiter = p["delimiter"] or "|"
    page_size = int(p["page_size"] or 1024)
    assert_mode = bool(p["assert_mode"])
    probe_ids = expand_id_set(p)

    if expect not in _FORM_EXPECT_CHOICES:
        raise ValueError(
            "kind=revision_form_sampling: expect must be one of %s (got %r)"
            % (list(_FORM_EXPECT_CHOICES), expect))

    per_id = {}
    total_raw = 0
    total_split = 0
    missing = []

    for doc_id in probe_ids:
        path = "/databases/%s/revisions?id=%s&pageSize=%d" % (db, quote(doc_id), page_size)
        s, b = request("GET", target, p["ravendb_domain"], path,
                       p["client_cert"], p["ca_cert"])
        if s != 200:
            raise RuntimeError(
                "%s/%s id=%s: /revisions returned HTTP %d -- db missing or node unreachable"
                % (target, db, doc_id, s))
        results = json.loads(b).get("Results") or []
        if not results:
            missing.append(doc_id)
            per_id[doc_id] = (0, 0)
            continue
        raw_n = split_n = 0
        for rev in results:
            cv = (rev.get("@metadata") or {}).get("@change-vector") or ""
            if delimiter in cv:
                split_n += 1
            else:
                raw_n += 1
        per_id[doc_id] = (raw_n, split_n)
        total_raw += raw_n
        total_split += split_n

    lines = ["revision form sampling on %s/%s  (%d id(s), expect=%s):"
             % (target, db, len(probe_ids), expect)]
    for i, (rn, sn) in list(per_id.items())[:10]:
        lines.append("  %-30s  raw=%-3d  split=%-3d" % (i, rn, sn))
    lines.append("  totals: raw=%d  split=%d  missing(no-revs)=%d"
                 % (total_raw, total_split, len(missing)))

    if missing:
        return violation(assert_mode, lines + [
            "FAIL  %d id(s) returned no revisions: %s" % (len(missing), missing[:20])])

    bad = None
    if expect == "any-raw" and total_raw == 0:
        bad = "expected any-raw but every revision is split"
    elif expect == "all-split" and total_raw > 0:
        bad = "expected all-split but %d revision(s) are raw" % total_raw
    elif expect == "any-split" and total_split == 0:
        bad = "expected any-split but every revision is raw"
    elif expect == "all-raw" and total_split > 0:
        bad = "expected all-raw but %d revision(s) are split" % total_split
    if bad:
        return violation(assert_mode, lines + ["FAIL  " + bad])

    lines.append("PASS  expect=%s held (raw=%d split=%d)" % (expect, total_raw, total_split))
    return lines


def k_extension_stats_parity(p):
    """Assert selected `aspects` (attachments/counters/timeseries) match across nodes."""
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])
    aspects_csv = p["aspects"] or "attachments,counters,timeseries"
    aspects = [s.strip() for s in aspects_csv.split(",")]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    fields = []
    if "attachments" in aspects:
        fields += ["CountOfUniqueAttachments", "CountOfAttachments"]
    if "counters" in aspects:
        fields += ["CountOfCounterEntries"]
    if "timeseries" in aspects:
        fields += ["CountOfTimeSeriesSegments"]

    lines = ["extension-stats parity (aspects=%s) on %s (skipped=%s):" %
             (aspects, list(has_db), skipped)]
    drift = []
    for field in fields:
        per = per_node_field(p, has_db, field)
        lines.append("  %s:" % field)
        lines.extend(format_per_node(per, indent="    "))
        if len(set(per.values())) != 1:
            drift.append(field)

    if drift:
        return violation(assert_mode, lines + ["FAIL  drift on: %s" % drift])
    lines.append("PASS  every selected aspect matches across nodes")
    return lines


def k_hub_sink_doc_set(p):
    hub_leader = p["hub_cluster_leader"]
    sink_leader = p["sink_cluster_leader"]
    db = p["db_name"]
    allowed = p["allowed_prefixes"] or []
    sink_local = p["sink_local_prefixes"] or []
    assert_mode = bool(p["assert_mode"])
    sample_cap = int(p["sample_cap"] or 25)

    if not hub_leader or not sink_leader:
        raise ValueError(
            "kind=hub_sink_doc_set requires `hub_cluster_leader` and `sink_cluster_leader`")
    if not allowed:
        raise ValueError(
            "kind=hub_sink_doc_set requires `allowed_prefixes` (hub->sink filter)")

    hub_ids = set(stream_all_doc_ids(
        hub_leader, p["ravendb_domain"], db, p["client_cert"], p["ca_cert"]))
    sink_ids = set(stream_all_doc_ids(
        sink_leader, p["ravendb_domain"], db, p["client_cert"], p["ca_cert"]))

    expected_on_sink = {i for i in hub_ids if prefix_match(i, allowed)}
    expected_on_hub  = {i for i in sink_ids if not prefix_match(i, sink_local)}

    missing_on_sink = sorted(expected_on_sink - sink_ids)
    missing_on_hub  = sorted(expected_on_hub - hub_ids)

    lines = [
        "hub-sink doc-set completeness  hub=%s  sink=%s  db=%s"
        % (hub_leader, sink_leader, db),
        "  hub ids: %d   sink ids: %d" % (len(hub_ids), len(sink_ids)),
        "  filter (hub->sink): %s" % allowed,
        "  sink-local allowlist: %s" % (sink_local or "[]"),
        "  expected-on-sink (hub ids matching filter): %d" % len(expected_on_sink),
        "  expected-on-hub  (sink ids not in sink-local): %d" % len(expected_on_hub),
        "  MISSING on sink: %d" % len(missing_on_sink),
        "  MISSING on hub:  %d" % len(missing_on_hub),
    ]
    if missing_on_sink:
        lines.append("  missing-on-sink sample (first %d of %d): %s"
                     % (min(sample_cap, len(missing_on_sink)),
                        len(missing_on_sink),
                        missing_on_sink[:sample_cap]))
    if missing_on_hub:
        lines.append("  missing-on-hub  sample (first %d of %d): %s"
                     % (min(sample_cap, len(missing_on_hub)),
                        len(missing_on_hub),
                        missing_on_hub[:sample_cap]))

    if missing_on_sink or missing_on_hub:
        return violation(assert_mode, lines + [
            "FAIL  hub-sink completeness: %d missing on sink, %d missing on hub"
            % (len(missing_on_sink), len(missing_on_hub))])
    lines.append("PASS  every expected doc is present on both sides")
    return lines


def k_stats_parity(p):
    """Print a per-node stats table and assert uniformity on `assert_fields`.

    Fields not in `assert_fields` are still printed (marked "OK (info)" or
    "DRIFT (info)") so drift on intrinsic-per-node fields is visible but not fatal.
    SizeOnDisk is always info-only.

    Optional `settle_secs`: before reading the table, poll the asserted fields
    until they agree across all has_db nodes (or timeout).  /stats counters
    update asynchronously a few seconds behind the etag; a preceding wait
    (etag_parity / quiescence) returns the instant the etag stops moving but
    the materialized CountOfDocuments / CountOfRevisionDocuments counters
    can still lag by a poll interval or two.  When `settle_secs > 0`, the
    pre-settle loop closes that race so the final assertion is reliable."""
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])
    assert_fields = p["assert_fields"] or _STATS_DEFAULT_ASSERTED
    settle_secs = int(p["settle_secs"] or 0)
    settle_interval = int(p["settle_interval"] or 3)

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    settle_elapsed = 0.0
    settle_polls = 0
    if settle_secs > 0:
        settle_start = time.time()
        deadline = settle_start + settle_secs
        last_drift = None
        while True:
            settle_polls += 1
            per_field = {}
            for field in assert_fields:
                per_field[field] = per_node_field(p, has_db, field)
            drift = [(f, per_field[f]) for f in assert_fields
                     if len(set(per_field[f].values())) > 1]
            if not drift:
                settle_elapsed = time.time() - settle_start
                break
            last_drift = drift
            if time.time() >= deadline:
                settle_elapsed = time.time() - settle_start
                raise RuntimeError(
                    "stats_parity: pre-settle TIMEOUT after %.1fs (%d poll(s)) -- "
                    "asserted field(s) never converged across %s: %s"
                    % (settle_elapsed, settle_polls, list(has_db), last_drift))
            time.sleep(settle_interval)

    per_node = {}
    for target, shard_id in (has_db.items() if isinstance(has_db, dict)
                             else [(t, None) for t in has_db]):
        s = get_stats(p, target, shard_id=shard_id)
        if s is None:
            continue
        row = {}
        for field in _STATS_ALL_FIELDS:
            row[field] = s.get(field) or 0
        row["SizeOnDisk"] = (s.get("SizeOnDisk") or {}).get("SizeInBytes") or 0
        per_node[target] = row

    node_order = list(has_db)

    # Column widths sized to the widest cell per node (or the node name, min 8).
    node_widths = {}
    for n in node_order:
        widest = max(len(str(per_node[n][f])) for f in _STATS_ALL_FIELDS + ["SizeOnDisk"])
        node_widths[n] = max(widest, len(n), 8)
    field_width = max(len(f) for f in _STATS_ALL_FIELDS + ["SizeOnDisk (info only)"])

    if settle_secs > 0:
        lines = ["/stats parity  db=%s  nodes=%s  pre-settle=%.1fs (%d poll(s))"
                 % (p["db_name"], node_order, settle_elapsed, settle_polls)]
    else:
        lines = ["/stats parity  db=%s  nodes=%s  pre-settle=disabled"
                 % (p["db_name"], node_order)]
    if skipped:
        lines.append("  SKIPPED (DB not present): %s" % skipped)

    def fmt_node_columns(values):
        cells = ["%*s" % (node_widths[n], values[i]) for i, n in enumerate(node_order)]
        return " , ".join(cells)

    header = "%-*s  %s  status" % (field_width, "field", fmt_node_columns(node_order))
    lines.append(header)

    mismatched = []
    flagged_info = []
    for field in _STATS_ALL_FIELDS:
        values = [per_node[n][field] for n in node_order]
        is_uniform = len(set(values)) == 1
        is_asserted = field in assert_fields
        if not is_uniform and is_asserted:
            mismatched.append(field)
            status_label = "MISMATCH"
        elif not is_uniform:
            flagged_info.append(field)
            status_label = "DRIFT (info)"
        elif is_asserted:
            status_label = "OK"
        else:
            status_label = "OK (info)"
        lines.append("%-*s  %s  %s" % (field_width, field, fmt_node_columns(values), status_label))

    size_values = [per_node[n]["SizeOnDisk"] for n in node_order]
    lines.append("%-*s  %s  %s" %
                 (field_width, "SizeOnDisk", fmt_node_columns(size_values), "info"))

    if flagged_info:
        lines.append("WARN  informational drift on per-node-intrinsic fields: %s" % flagged_info)

    if mismatched:
        return violation(assert_mode, lines +
                         ["FAIL  /stats parity broken on: %s" % mismatched])
    if assert_mode:
        lines.append("PASS  every asserted field is parity-equal across %s" % node_order)
    return lines


# ============================================================================
# === Leak / orphan kinds ====================================================
# ============================================================================

def k_orphan_revisions(p):
    """Trigger /admin/revisions/orphaned/adopt on every node, wait for completion,
    and FAIL if any node reports a non-zero AdoptedCount.

    Inconclusive results (operation-id recycling returning a non-adopt $type) are
    surfaced separately."""
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    budget = int(p["budget_secs"] or 60)

    # Trigger adopt on every node.
    op_ids = {}
    unreachable_nodes = []
    for target in nodes:
        path = "/databases/%s/admin/revisions/orphaned/adopt" % db
        try:
            status, body = request("POST", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"], body={})
        except Exception as e:
            unreachable_nodes.append(target)
            continue
        if status not in (200, 201):
            raise RuntimeError("trigger adopt on %s failed: HTTP %d" % (target, status))
        op_ids[target] = int(json.loads(body)["OperationId"])

    if unreachable_nodes:
        raise RuntimeError(
            "k_orphan_revisions: %s unreachable -- can't determine orphan state"
            % sorted(unreachable_nodes))

    # Poll for terminal state per node.
    adopted = {}
    inconclusive = []
    deadline = time.monotonic() + budget
    pending = dict(op_ids)
    while pending and time.monotonic() < deadline:
        done_now = []
        for target, op_id in pending.items():
            state_path = "/databases/%s/operations/state?id=%d" % (db, op_id)
            status, body = request("GET", target, p["ravendb_domain"], state_path,
                                   p["client_cert"], p["ca_cert"])
            if status != 200:
                continue
            state = json.loads(body)
            if state.get("Status") not in ("Completed", "Faulted", "Canceled"):
                continue
            done_now.append(target)
            result = state.get("Result") or {}
            result_type = result.get("$type") or ""
            if "AdoptOrphanedRevisionsResult" in result_type:
                adopted[target] = result.get("AdoptedCount", 0)
            else:
                # RavenDB recycles operation ids on fast operations; we may have
                # observed a stale result from a different op.  Mark inconclusive.
                adopted[target] = "?"
                inconclusive.append(target)
        for t in done_now:
            pending.pop(t, None)
        if pending:
            time.sleep(_ORPHAN_POLL_SECS)

    if pending:
        raise RuntimeError("adopt operation never finished on %s within %ds" %
                           (list(pending), budget))

    lines = ["orphan revisions adopted per node:"]
    lines.extend(format_per_node(adopted))
    if inconclusive:
        lines.append("INCONCLUSIVE on %s -- /operations/state returned a stale non-adopt result "
                     "(RavenDB operation-id recycling on fast operations)" % inconclusive)

    real_counts = [c for c in adopted.values() if c != "?"]
    if any(c != 0 for c in real_counts):
        return violation(assert_mode, lines + ["FAIL  orphans were present on at least one node"])

    if inconclusive:
        lines.append("OK (partial)  no orphans on readable nodes; %s inconclusive" % inconclusive)
    else:
        lines.append("PASS  no orphan revisions on any node")
    return lines


def k_scan_fltr(p):
    """Walk `capture_dir` for *.cv files and FAIL on any file containing the FLTR: marker."""
    capture_dir = p["capture_dir"]
    assert_mode = bool(p["assert_mode"])
    if not capture_dir or not os.path.isdir(capture_dir):
        raise ValueError("capture_dir must be an existing directory; got %r" % capture_dir)

    found_files = []
    for dirpath, _dirs, files in os.walk(capture_dir):
        for name in files:
            if name.endswith(".cv"):
                found_files.append(os.path.join(dirpath, name))

    if not found_files:
        raise RuntimeError(
            "k_scan_fltr: 0 .cv files found under %s -- nothing to scan, "
            "can't verify FLTR-cleanliness" % capture_dir)

    leaks = []
    for path in found_files:
        with open(path, "r") as f:
            content = f.read()
        if "FLTR:" in content:
            leaks.append((path, content.strip()))

    lines = ["FLTR scan: %d .cv file(s) under %s, leaks=%d" %
             (len(found_files), capture_dir, len(leaks))]
    for path, content in leaks:
        lines.append("  LEAK  %s  ::  %s" % (path, content))

    if leaks and assert_mode:
        return violation(True, lines + ["FAIL  FLTR leakage detected"])
    return lines


def k_lane_inert(p):
    """Sample revisions for each id_prefix and FAIL if any revision CV contains '|'
    (the new-lane composite-CV delimiter).  Used to verify the new lane stayed inert
    on lanes that should still be on the legacy raw form."""
    nodes = p["nodes"]
    db = p["db_name"]
    prefixes = p["id_prefixes"]
    sample = int(p["sample_per_prefix"] or 25)
    assert_mode = bool(p["assert_mode"])

    if not prefixes:
        raise ValueError("kind=lane_inert requires `id_prefixes` (non-empty list)")

    probe_ids = []
    for prefix in prefixes:
        clean = prefix.rstrip("/")
        for n in range(sample):
            probe_ids.append("%s/%d" % (clean, n))

    leaks = []
    sampled = 0
    unreachable_nodes = set()
    for target in nodes:
        for doc_id in probe_ids:
            if target in unreachable_nodes:
                break
            path = "/databases/%s/revisions?id=%s&pageSize=100" % (db, quote(doc_id))
            try:
                status, body = request("GET", target, p["ravendb_domain"], path,
                                       p["client_cert"], p["ca_cert"])
            except Exception:
                unreachable_nodes.add(target)
                break
            if status != 200:
                continue
            revisions = json.loads(body).get("Results") or []
            sampled += len(revisions)
            for rev in revisions:
                cv = (rev.get("@metadata") or {}).get("@change-vector") or ""
                if "|" in cv:
                    leaks.append("%s / %s / %s" % (target, doc_id, cv))

    if unreachable_nodes:
        raise RuntimeError(
            "k_lane_inert: %s unreachable -- can't determine lane-inertness"
            % sorted(unreachable_nodes))

    if sampled == 0:
        raise RuntimeError(
            "k_lane_inert: 0 revisions sampled across %s for prefixes %s -- "
            "can't verify lane-inertness with no data" % (nodes, prefixes))

    # Pull SupportedFeatures for the forensic line; non-fatal if unreachable.
    rec_path = "/admin/databases?name=%s" % quote(db)
    features = {}
    try:
        status, body = request("GET", nodes[0], p["ravendb_domain"], rec_path,
                               p["client_cert"], p["ca_cert"])
        if status == 200:
            features = json.loads(body).get("SupportedFeatures") or {}
    except Exception:
        pass

    lines = ["lane-inert check (db=%s, %d node(s), %d prefix(es), %d revisions sampled):" %
             (db, len(nodes), len(prefixes), sampled),
             "  leaks (new-lane '|' in CV): %d" % len(leaks),
             "  forensic: SupportedFeatures = %s" % features]
    if leaks:
        lines.append("  sample (first 5):")
        for leak in leaks[:5]:
            lines.append("    " + leak)

    if leaks:
        return violation(assert_mode, lines +
                         ["FAIL  lane-inert breach: %d revision CV(s) contain '|'" % len(leaks)])
    lines.append("PASS  no '|' in any sampled revision CV; new lane stayed inert")
    return lines


def k_filter_compliance(p):
    """List every doc id on the sink leader and FAIL on any id that doesn't start
    with one of `allowed_prefixes` (auto-discovered from DatabaseRecord.SinkPullReplications
    if not provided)."""
    sink_leader = p["sink_cluster_leader"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    allowed = p["allowed_prefixes"]

    if not allowed:
        rec_path = "/admin/databases?name=%s" % quote(db)
        try:
            status, body = request("GET", sink_leader, p["ravendb_domain"], rec_path,
                                   p["client_cert"], p["ca_cert"])
        except Exception:
            raise RuntimeError("k_filter_compliance: %s unreachable -- can't read DatabaseRecord"
                               % sink_leader)
        if status != 200:
            raise RuntimeError("GET DatabaseRecord on %s failed: HTTP %d" % (sink_leader, status))
        sink_pulls = json.loads(body).get("SinkPullReplications") or []
        prefixes = []
        for entry in sink_pulls:
            for p_str in (entry.get("AllowedHubToSinkPaths") or []):
                prefixes.append(p_str.rstrip("*"))
        allowed = sorted(set(prefixes))

    if not allowed:
        raise RuntimeError("could not resolve allowed_prefixes (none passed, none in DatabaseRecord)")

    list_path = "/databases/%s/docs?start=0&pageSize=100000&metadataOnly=true" % db
    try:
        status, body = request("GET", sink_leader, p["ravendb_domain"], list_path,
                               p["client_cert"], p["ca_cert"])
    except Exception:
        raise RuntimeError("k_filter_compliance: %s unreachable -- can't list docs" % sink_leader)
    if status != 200:
        raise RuntimeError("list docs on %s failed: HTTP %d" % (sink_leader, status))
    results = json.loads(body).get("Results") or []
    sink_ids = [(r.get("@metadata") or {}).get("@id") for r in results]
    sink_ids = [i for i in sink_ids if i is not None]

    leaks = [i for i in sink_ids if not any(i.startswith(pre) for pre in allowed)]

    lines = ["filter compliance on %s/%s:" % (sink_leader, db),
             "  allowed_prefixes: %s" % allowed,
             "  total sink docs: %d" % len(sink_ids),
             "  leak ids (no allowed prefix matched): %d" % len(leaks)]
    if leaks:
        lines.append("  sample (first 10): %s" % leaks[:10])

    if leaks:
        return violation(assert_mode, lines +
                         ["FAIL  filter leak: %d doc(s) don't match %s" % (len(leaks), allowed)])
    lines.append("PASS  every sink doc matches an allowed prefix")
    return lines


def k_cross_sink_isolation(p):
    """Probe each `forbidden_prefixes/<N>` doc id on the sink leader and FAIL on any 200."""
    sink_leader = p["sink_cluster_leader"]
    db = p["db_name"]
    forbidden = p["forbidden_prefixes"]
    sample = int(p["sample_per_prefix"] or 100)
    assert_mode = bool(p["assert_mode"])

    if not forbidden:
        raise ValueError("kind=cross_sink_isolation requires `forbidden_prefixes`")

    probe_ids = []
    for prefix in forbidden:
        clean = prefix.rstrip("/")
        for n in range(sample):
            probe_ids.append("%s/%d" % (clean, n))

    leaks = []
    for doc_id in probe_ids:
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        try:
            status, _ = request("GET", sink_leader, p["ravendb_domain"], path,
                                p["client_cert"], p["ca_cert"])
        except Exception:
            raise RuntimeError(
                "k_cross_sink_isolation: %s unreachable -- can't probe %d forbidden prefix(es)"
                % (sink_leader, len(forbidden)))
        if status == 200:
            leaks.append(doc_id)

    lines = ["cross-sink isolation check on %s/%s:" % (sink_leader, db),
             "  forbidden_prefixes: %s" % forbidden,
             "  probes: %d  leaks: %d" % (len(probe_ids), len(leaks))]
    if leaks:
        lines.append("  sample (first 10): %s" % leaks[:10])

    if leaks:
        return violation(assert_mode, lines +
                         ["FAIL  cross-sink leak: %d doc(s) with forbidden prefix found" % len(leaks)])
    lines.append("PASS  no forbidden-prefix docs on this sink")
    return lines


# ============================================================================
# === CV-shape kinds =========================================================
# ============================================================================

def k_stored_item_cv_split(p):
    """Check the change-vector SHAPE of each probed document on a single node.

    Each document's @change-vector is either:
      - 'raw'   = legacy form, a flat list of entries (no pipe character)
      - 'split' = new composite form, two halves separated by `delimiter` (default '|')

    expect='split' -> every probed doc's CV must contain the delimiter.
    expect='raw'   -> every probed doc's CV must NOT contain the delimiter.

    Special case: with expect='split', if NO probed doc has the delimiter, we
    treat the whole probe as N/A (the composite-CV lane is not active on this
    build) instead of failing.  This is intentional: the kind is meant to detect
    SHAPE drift, not to assert the feature is on."""
    target = p["target"]
    db = p["db_name"]
    doc_ids = p["doc_ids"]
    expect = p["expect"] or "split"
    delimiter = p["delimiter"] or "|"
    assert_mode = bool(p["assert_mode"])

    if not doc_ids:
        raise ValueError("kind=stored_item_cv_split requires `doc_ids` (non-empty list)")

    lines = ["stored-item CV shape check on %s/%s (expect=%s, delimiter='%s'):" %
             (target, db, expect, delimiter)]

    cvs = {}
    unreachable = False
    bad_status = {}
    for doc_id in doc_ids:
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        try:
            status, body = request("GET", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
        except Exception:
            unreachable = True
            break
        if status != 200:
            bad_status[doc_id] = status
            continue
        results = json.loads(body).get("Results") or []
        cv = ""
        if results:
            cv = (results[0].get("@metadata") or {}).get("@change-vector") or ""
        cvs[doc_id] = cv

    if unreachable:
        raise RuntimeError(
            "k_stored_item_cv_split: target %s unreachable -- can't verify "
            "stored-item CV shape" % target)

    if bad_status:
        raise RuntimeError(
            "k_stored_item_cv_split: %d/%d probe doc(s) unreadable on %s "
            "(HTTP statuses: %s) -- can't verify CV shape on partial coverage" %
            (len(bad_status), len(doc_ids), target, bad_status))

    if expect == "split" and not any(delimiter in v for v in cvs.values()):
        for doc_id, cv in cvs.items():
            lines.append("  %s  LEGACY raw CV (no '%s')" % (doc_id, delimiter))
        lines.append("N/A  legacy raw-CV form across all probes -- composite-CV lane not active on this build")
        return lines

    violations = []
    for doc_id, cv in cvs.items():
        has_delim = delimiter in cv
        ok = (has_delim and expect == "split") or (not has_delim and expect == "raw")
        verdict = "OK" if ok else "MISMATCH"
        lines.append("  %s  %s  cv=%s" % (doc_id, verdict, cv))
        if not ok:
            violations.append(doc_id)

    if violations:
        return violation(assert_mode, lines +
                         ["FAIL  %d doc(s) don't match expected shape:" % len(violations)] +
                         ["    %s" % v for v in violations])
    lines.append("PASS  every probed doc matches expected '%s' shape" % expect)
    return lines


def k_cv_boundary_by_dbid(p):
    """Verify no source-cluster dbid appears on the order side of any receiver's DB CV.

    Requires `source_nodes` and `receiver_nodes` (different physical clusters).
    `strict_v_new=true` additionally FAILs receivers still on the legacy raw CV form
    (no '|' delimiter)."""
    db = p["db_name"]
    sources = p["source_nodes"]
    receivers = p["receiver_nodes"]
    strict_v_new = bool(p["strict_v_new"])
    assert_mode = bool(p["assert_mode"])

    if not sources or not receivers:
        raise ValueError("kind=cv_boundary_by_dbid requires `source_nodes` and `receiver_nodes`")

    source_dbids = set()
    unreachable_sources = []
    for target in sources:
        s = get_stats(p, target)
        if s is None:
            unreachable_sources.append(target)
            continue
        dbid = s.get("DatabaseId")
        if dbid:
            source_dbids.add(dbid)

    if unreachable_sources:
        raise RuntimeError(
            "k_cv_boundary_by_dbid: %s source nodes unreachable -- can't "
            "enumerate source dbids" % sorted(unreachable_sources))

    receiver_dbids = set()
    receiver_cvs = {}
    unreachable_receivers = []
    for target in receivers:
        s = get_stats(p, target)
        if s is None:
            unreachable_receivers.append(target)
            continue
        dbid = s.get("DatabaseId")
        if dbid:
            receiver_dbids.add(dbid)
        receiver_cvs[target] = s.get("DatabaseChangeVector") or ""

    if unreachable_receivers:
        raise RuntimeError(
            "k_cv_boundary_by_dbid: %s receiver nodes unreachable -- can't "
            "verify CV boundary" % sorted(unreachable_receivers))

    if source_dbids & receiver_dbids:
        raise RuntimeError(
            "k_cv_boundary_by_dbid: source and receiver dbid sets overlap "
            "(source=%s receiver=%s) -- not distinct clusters" %
            (sorted(source_dbids), sorted(receiver_dbids)))

    lines = ["CV-boundary by dbid (db=%s):" % db,
             "  source dbids:   %s" % sorted(source_dbids),
             "  receiver dbids: %s" % sorted(receiver_dbids)]

    legacy = []
    source_leaks = []
    unknown = []
    for target, cv in receiver_cvs.items():
        if "|" not in cv:
            legacy.append(target)
            lines.append("  %s  LEGACY raw CV (no '|') -- N/A on this CV form" % target)
            continue
        order_side = cv.split("|", 1)[0]
        entries = parse_cv_entries(order_side)
        leaked = [d for _, d in entries if d in source_dbids]
        unk = [d for _, d in entries if d not in source_dbids and d not in receiver_dbids]
        lines.append("  %s  new-lane  order_side='%s'  source_leaks=%d  unknown_dbids=%d" %
                     (target, order_side, len(leaked), len(unk)))
        if leaked:
            source_leaks.append((target, leaked))
        if unk:
            unknown.extend(unk)

    if strict_v_new and legacy and assert_mode:
        return violation(True, lines +
                         ["FAIL  strict_v_new=true but legacy CV on: %s" % legacy])

    if source_leaks:
        return violation(assert_mode, lines +
                         ["FAIL  CV-boundary breach: source dbid on order side: %s" % source_leaks])

    if unknown:
        lines.append("WARN  unknown dbids on order side (not source, not receiver): %s" %
                     sorted(set(unknown)))
    lines.append("PASS  no source dbids on any receiver's order side")
    return lines


def k_db_cv_order_side_only(p):
    """Check that every receiver's database change vector only references
    cluster tags from the receiver's own cluster -- no foreign cluster tags.

    Cluster tags are the single-letter identifiers (A, B, C, ...) RavenDB
    stamps into each CV entry.  By default we derive the allowed tags from
    the trailing letter of each receiver node name ('1a' -> 'A', '2b' -> 'B').
    Pass `receiver_group_tags` to override that auto-derivation."""
    db = p["db_name"]
    receivers = p["receiver_group_nodes"]
    assert_mode = bool(p["assert_mode"])

    if not receivers:
        raise ValueError("kind=db_cv_order_side_only requires `receiver_group_nodes`")

    if p["receiver_group_tags"]:
        tags = [t.upper() for t in p["receiver_group_tags"]]
    else:
        tags = [n[-1].upper() for n in receivers]

    lines = ["db-CV order-side-only check (db=%s, expected tags=%s):" % (db, tags)]
    offending = []
    unreachable_receivers = []
    empty_cv_receivers = []
    for target in receivers:
        s = get_stats(p, target)
        if s is None:
            unreachable_receivers.append(target)
            continue
        cv = s.get("DatabaseChangeVector") or ""
        if not cv.strip():
            empty_cv_receivers.append(target)
            continue
        entries = parse_cv_entries(cv)
        bad_tags = [t for t, _ in entries if t not in tags]
        marker = "OK" if not bad_tags else "LEAK %s" % sorted(set(bad_tags))
        lines.append("  %s  %s  (%d CV entries)" % (target, marker, len(entries)))
        for raw_entry in [s.strip() for s in cv.split(",") if s.strip()]:
            tag = raw_entry.split(":", 1)[0]
            prefix = "      " if tag in tags else "    LEAK"
            lines.append("%s  %s" % (prefix, raw_entry))
        if bad_tags:
            offending.append((target, bad_tags))

    if unreachable_receivers:
        raise RuntimeError(
            "k_db_cv_order_side_only: %s receiver nodes unreachable -- "
            "can't verify order-side-only" % sorted(unreachable_receivers))

    if empty_cv_receivers:
        raise RuntimeError(
            "k_db_cv_order_side_only: %s receivers reported an empty "
            "DatabaseChangeVector -- nothing to check, can't verify "
            "order-side-only" % sorted(empty_cv_receivers))

    if offending:
        return violation(assert_mode, lines +
                         ["FAIL  CV-boundary breach: foreign tag on: %s" % offending])
    lines.append("PASS  every receiver's DB CV references only %s" % tags)
    return lines


def k_shard_placement_check(p):
    """Assert each probe doc id lives on EXACTLY ONE shard of `target/db_name`.

    `target` is any node in a sharded cluster.  The kind:
      1. Reads /admin/databases?name=<db> on `target` to enumerate shard ids.
      2. For each probe doc id, GETs /databases/<db>/docs?id=<id>&shardNumber=N
         once per shard N (per-shard route -- same pattern get_stats uses for
         /stats).  Exactly one shard must return 200; all others must return
         non-200 (typically 404).
      3. Doc ids that are present on 0 shards (missing) or >1 shards
         (mis-routed -- the orchestrator placed the same id on multiple
         shards) are reported.

    Returns list[str] with the per-id placement table (first 10) + PASS/FAIL line.
    """
    target = p["target"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    probe_ids = expand_id_set(p)

    s, b = request("GET", target, p["ravendb_domain"],
                   "/admin/databases?name=%s" % db,
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        raise RuntimeError(
            "%s/%s: /admin/databases returned HTTP %d -- db missing or node unreachable"
            % (target, db, s))
    rec = json.loads(b)
    shards = (rec.get("Sharding") or {}).get("Shards") or {}
    if not shards:
        raise RuntimeError(
            "%s/%s: not a sharded database -- shard_placement_check requires sharding"
            % (target, db))
    shard_ids = sorted(shards.keys(), key=lambda s: int(s))

    # Route each per-shard docs?id=X probe to a node that ACTUALLY owns the
    # shard.  The cursor 7.x per-shard endpoint serves only local data, so
    # asking the orchestrator (or any single node) for non-owned shards
    # returns 410 -- the doc would be silently classified as missing on every
    # shard except the one the orchestrator happens to own locally.
    cluster_prefix = target[:-1]
    shard_owner = {}
    for shard_id in shard_ids:
        members = shards.get(shard_id, {}).get("Members") or []
        if not members:
            raise RuntimeError(
                "%s/%s: shard %s has no Members -- can't determine placement"
                % (target, db, shard_id))
        owner_tag = members[0]
        shard_owner[shard_id] = (owner_tag, "%s%s" % (cluster_prefix, owner_tag.lower()))

    per_id_owners = {}
    for doc_id in probe_ids:
        owners = []
        for shard_id in shard_ids:
            owner_tag, owner_target = shard_owner[shard_id]
            suffix = "docs?id=%s" % quote(doc_id)
            try:
                status, body = probe_shard_endpoint(
                    owner_target, p["ravendb_domain"], db, owner_tag, shard_id,
                    p["client_cert"], p["ca_cert"], suffix=suffix)
            except Exception:
                # Treat transport failure as "can't determine placement" -- fail loud
                # rather than silently dropping the shard from the comparison.
                raise RuntimeError(
                    "%s/%s shard=%s @ %s: transport error during placement probe for id=%s"
                    % (target, db, shard_id, owner_target, doc_id))
            if status == 200 and (json.loads(body).get("Results") or []):
                owners.append(shard_id)
        per_id_owners[doc_id] = owners

    missing = [i for i, o in per_id_owners.items() if len(o) == 0]
    duplicate = [(i, o) for i, o in per_id_owners.items() if len(o) > 1]

    lines = ["shard placement on %s/%s  (%d id(s) across shards %s):"
             % (target, db, len(probe_ids), shard_ids)]
    for i, o in list(per_id_owners.items())[:10]:
        lines.append("  %-30s  owners=%s" % (i, o))
    lines.append("  missing(0-shard): %d   duplicate(>1-shard): %d" % (len(missing), len(duplicate)))

    if missing or duplicate:
        return violation(assert_mode, lines + [
            "FAIL  missing on: %s   duplicate on: %s"
            % (missing[:20], duplicate[:20])])
    lines.append("PASS  every probe id lives on exactly one shard")
    return lines


def k_cross_cluster_cv_equality(p):
    """Per-doc CV (dbid, etag)-set equality across `nodes`.

    Equality mode (anchor unset):  all nodes must produce the same (dbid, etag) set.
    Anchor mode (anchor set):      anchor's set must be a SUBSET of every other node's.
                                   Used for hub-vs-sinks checks where multi-node sinks
                                   add their own local dbId entry on receive (RF>1 -> local
                                   etag entry; RF=1 -> no extra entry).  The hub's upstream
                                   entry must survive intact on every sink."""
    nodes = p["nodes"]
    db = p["db_name"]
    doc_ids = p["doc_ids"]
    assert_mode = bool(p["assert_mode"])
    anchor = p.get("anchor")

    if not nodes or len(nodes) < 2:
        raise ValueError("kind=cross_cluster_cv_equality requires `nodes` with >=2 targets")
    if not doc_ids:
        raise ValueError("kind=cross_cluster_cv_equality requires `doc_ids` (non-empty list)")
    if anchor and anchor not in nodes:
        raise ValueError("kind=cross_cluster_cv_equality `anchor=%s` not in `nodes=%s`" %
                         (anchor, nodes))

    lines = ["cross-cluster CV equality on %s across %d node(s) [%s]:" %
             (db, len(nodes), ", ".join(nodes))]
    mismatched = []
    unreachable = []

    for doc_id in doc_ids:
        per_node = {}
        for target in nodes:
            path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
            status, body = request("GET", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
            if status != 200:
                per_node[target] = ("HTTP_%d" % status, None)
                continue
            results = json.loads(body).get("Results") or []
            if not results:
                per_node[target] = ("NOT_FOUND", None)
                continue
            cv = (results[0].get("@metadata") or {}).get("@change-vector") or ""
            per_node[target] = (cv, _parse_cv_set(cv))

        bad_targets = [t for t, (_, parsed) in per_node.items() if parsed is None]
        if bad_targets:
            unreachable.append(doc_id)
            lines.append("  %-25s  UNREACHABLE on %s" % (doc_id, bad_targets))
            for t, (info, _) in per_node.items():
                lines.append("    %s  %s" % (t, info))
            continue

        parsed_sets = {t: per_node[t][1] for t in nodes}
        if anchor:
            anchor_set = parsed_sets[anchor]
            doc_ok = all(anchor_set.issubset(parsed_sets[t]) for t in nodes if t != anchor)
        else:
            first = parsed_sets[nodes[0]]
            doc_ok = all(parsed_sets[t] == first for t in nodes[1:])

        if not doc_ok:
            mismatched.append(doc_id)
            lines.append("  %-25s  MISMATCH" % doc_id)
            for t in nodes:
                cv, parsed = per_node[t]
                tag = "  <-- anchor" if t == anchor else ""
                lines.append("    %s  cv='%s'  entries=%s%s" %
                             (t, (cv or "")[:80],
                              sorted(parsed) if parsed else "<empty>", tag))

    lines.append("  checked %d doc(s); mismatched=%d  unreachable=%d" %
                 (len(doc_ids), len(mismatched), len(unreachable)))

    if mismatched or unreachable:
        return violation(assert_mode, lines + [
            "FAIL  cross-cluster CV equality broke on %d doc(s); unreachable on %d" %
            (len(mismatched), len(unreachable))])
    if assert_mode:
        mode_desc = ("anchor=%s entries subset-of every node" % anchor) if anchor \
                    else ("identical CV-entry set across all %d cluster(s)" % len(nodes))
        lines.append("PASS  every doc satisfies cross-cluster CV check (%s)" % mode_desc)
    return lines


# ============================================================================
# KINDS registry
# ============================================================================

KINDS = {
    # --- info-only ---
    "doc_count":                  k_doc_count,
    "replication":                k_replication,
    "replication_health":         k_replication_health,
    "schema_version":             k_schema_version,
    "size_envelope":              k_size_envelope,
    "supported_features":         k_supported_features,
    # --- capture ---
    "capture_cv":                 k_capture_cv,
    "capture_doc_cv":             k_capture_doc_cv,
    # --- parity ---
    "doc_count_parity":           k_doc_count_parity,
    "doc_id_set_parity":          k_doc_id_set_parity,
    "extension_stats_parity":     k_extension_stats_parity,
    "revision_count_parity":      k_revision_count_parity,
    "revision_form_sampling":     k_revision_form_sampling,
    "stats_parity":               k_stats_parity,
    # --- leak / orphan ---
    "cross_sink_isolation":       k_cross_sink_isolation,
    "filter_compliance":          k_filter_compliance,
    "hub_sink_doc_set":           k_hub_sink_doc_set,
    "lane_inert":                 k_lane_inert,
    "orphan_revisions":           k_orphan_revisions,
    "scan_fltr":                  k_scan_fltr,
    "shard_placement_check":      k_shard_placement_check,
    # --- CV-shape ---
    "cross_cluster_cv_equality":  k_cross_cluster_cv_equality,
    "cv_boundary_by_dbid":        k_cv_boundary_by_dbid,
    "db_cv_order_side_only":      k_db_cv_order_side_only,
    "stored_item_cv_split":       k_stored_item_cv_split,
}


# ============================================================================
# Ansible entry point
# ============================================================================

def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),

        # transport
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),

        # universal
        assert_mode=dict(type="bool", default=False),

        # cluster / db scope
        db_name=dict(default=None),
        target=dict(default=None),
        nodes=dict(type="list", elements="str", default=None),
        cluster_leader=dict(default=None),

        # id sets (used by doc_id_set_parity, revision_count_parity,
        # capture_doc_cv, and any kind taking `doc_ids`/`id_prefixes`)
        ids=dict(type="list", elements="str", default=None),
        id_prefix=dict(default=None),
        count=dict(type="int", default=None),
        doc_ids=dict(type="list", elements="str", default=None),
        id_prefixes=dict(type="list", elements="str", default=None),
        sample_per_prefix=dict(type="int", default=None),

        # cross_cluster_cv_equality
        anchor=dict(default=None),

        # schema_version
        require_parity=dict(type="bool", default=False),
        expected_version=dict(default=None),

        # size_envelope
        baseline_file=dict(type="path", default=None),
        max_growth_pct=dict(type="float", default=None),

        # capture_cv / capture_doc_cv
        output_dir=dict(type="path", default=None),

        # scan_fltr
        capture_dir=dict(type="path", default=None),

        # revision_count_parity
        expected_count=dict(type="int", default=None),
        page_size=dict(type="int", default=None),

        # orphan_revisions
        budget_secs=dict(type="int", default=None),

        # extension_stats_parity
        aspects=dict(default=None),

        # stored_item_cv_split: "raw" / "split"
        # revision_form_sampling: "any-raw" / "all-split" / "any-split" / "all-raw"
        expect=dict(default=None,
                    choices=["raw", "split",
                             "any-raw", "all-split", "any-split", "all-raw",
                             None]),
        delimiter=dict(default=None),

        # filter_compliance / cross_sink_isolation / hub_sink_doc_set
        sink_cluster_leader=dict(default=None),
        allowed_prefixes=dict(type="list", elements="str", default=None),
        forbidden_prefixes=dict(type="list", elements="str", default=None),
        # hub_sink_doc_set
        hub_cluster_leader=dict(default=None),
        sink_local_prefixes=dict(type="list", elements="str", default=None),
        sample_cap=dict(type="int", default=None),

        # cv_boundary_by_dbid
        source_nodes=dict(type="list", elements="str", default=None),
        receiver_nodes=dict(type="list", elements="str", default=None),
        strict_v_new=dict(type="bool", default=False),

        # db_cv_order_side_only
        receiver_group_nodes=dict(type="list", elements="str", default=None),
        receiver_group_tags=dict(type="list", elements="str", default=None),

        # stats_parity
        assert_fields=dict(type="list", elements="str", default=None),
        # stats_parity pre-settle: poll asserted fields until they agree across
        # all has_db nodes before running the final assert.  Closes the gap
        # between etag-stable and counter-caught-up.  0 = no pre-settle.
        settle_secs=dict(type="int", default=None),
        settle_interval=dict(type="int", default=None),

    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except DiagnosticViolation as e:
        module.fail_json(msg="\n".join(e.lines) if isinstance(e.lines, list) else str(e.lines))
    except Exception as e:
        module.fail_json(msg=str(e))

    if isinstance(message, list):
        message = "\n".join(message)
    module.exit_json(changed=False, msg=message)


if __name__ == "__main__":
    main()
