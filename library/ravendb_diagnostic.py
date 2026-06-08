#!/usr/bin/python

import base64
import json
import os
import re
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import request, request_per_node


# ---------------------------------------------------------------------------- helpers

def _resolve_shard_for_tag(p, target, tag):
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
        else:
            skipped.append(target)

    for target in sharded_probe_needed:
        tag = target[-1].upper()
        shard_id = _resolve_shard_for_tag(p, target, tag)
        if shard_id is None:
            skipped.append(target)
            continue
        per = "/databases/%s/stats?nodeTag=%s&shardNumber=%s" % (db, tag, shard_id)
        s, _ = request("GET", target, p["ravendb_domain"], per,
                       p["client_cert"], p["ca_cert"])
        if s == 200:
            host_map[target] = shard_id
        else:
            skipped.append(target)

    return host_map, skipped


def get_stats(p, target, shard_id=None):
    db = p["db_name"]
    if shard_id is not None:
        tag = target[-1].upper()
        path = "/databases/%s/stats?nodeTag=%s&shardNumber=%s" % (db, tag, shard_id)
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        return json.loads(body) if status == 200 else None

    path = "/databases/%s/stats" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status == 200:
        return json.loads(body)
    if status == 500 and b"nodeTag is mandatory" in body:
        return aggregate_sharded_stats(p, target, db)
    return None


def aggregate_sharded_stats(p, target, db):
    rec_path = "/admin/databases?name=%s" % db
    s, b = request("GET", target, p["ravendb_domain"], rec_path,
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        return None
    sharding = (json.loads(b).get("Sharding") or {})
    shards = sharding.get("Shards") or {}
    if not shards:
        return None
    aggregate = {}
    for shard_id, shard_rec in shards.items():
        members = shard_rec.get("Members") or []
        if not members:
            continue
        per_path = "/databases/%s/stats?nodeTag=%s&shardNumber=%s" % (db, members[0], shard_id)
        s, b = request("GET", target, p["ravendb_domain"], per_path,
                       p["client_cert"], p["ca_cert"])
        if s != 200:
            continue
        for k, v in json.loads(b).items():
            if isinstance(v, int):
                aggregate[k] = aggregate.get(k, 0) + v
            else:
                aggregate.setdefault(k, v)
    return aggregate or None


def per_node_field(p, nodes_or_map, field):
    db = p["db_name"]
    out = {}
    if isinstance(nodes_or_map, dict):
        for target, shard_id in nodes_or_map.items():
            if shard_id is None:
                path = "/databases/%s/stats" % db
            else:
                tag = target[-1].upper()
                path = "/databases/%s/stats?nodeTag=%s&shardNumber=%s" % (db, tag, shard_id)
            s, b = request("GET", target, p["ravendb_domain"], path,
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


def format_per_node(per_node, indent="  "):
    if not per_node:
        return [indent + "(empty)"]
    width = max(len(n) for n in per_node)
    lines = []
    for name in sorted(per_node):
        lines.append("%s%-*s  %s" % (indent, width, name, per_node[name]))
    return lines


class DiagnosticViolation(Exception):
    def __init__(self, lines):
        self.lines = lines
        super().__init__("\n".join(lines))


def violation(assert_mode, lines):
    """If assert_mode is on, raise with `lines` carried as a list (so main() can
    fail_json with a multi-line msg that Ansible debug renders nicely).  Otherwise
    return them so the handler can include them in its info output."""
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


def k_doc_count(p):
    target = p["target"]
    db = p["db_name"]
    stats = get_stats(p, target)
    if stats is None:
        return "%s/%s  ->  (no /stats response)" % (target, db)
    return "%s/%s  ->  %s docs" % (target, db, stats.get("CountOfDocuments"))


def k_replication(p):
    target = p["target"]
    db = p["db_name"]
    path = "/databases/%s/replication/active-connections" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        return "replication  %s/%s  HTTP %d" % (target, db, status)
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


def k_replication_outgoing_count(p):
    target = p["target"]
    db = p["db_name"]
    counter_kind = p["counter_kind"] or "document"
    snapshot_path = p["snapshot_path"]
    destination_filter = p["destination_filter"]
    assert_mode = bool(p["assert_mode"])

    field_by_kind = {
        "document":    "DocumentOutputCount",
        "revision":    "RevisionOutputCount",
        "attachment":  "AttachmentOutputCount",
        "counter":     "CounterOutputCount",
        "time_series": "TimeSeriesSegmentsOutputCount",
    }
    if counter_kind not in field_by_kind:
        raise ValueError("counter_kind must be one of: %s" % sorted(field_by_kind))
    field = field_by_kind[counter_kind]

    path = "/databases/%s/replication/performance" % db
    status, body = request("GET", target, p["ravendb_domain"], path,
                           p["client_cert"], p["ca_cert"])
    if status != 200:
        raise RuntimeError("GET %s on %s returned HTTP %d (body=%r)" %
                           (path, target, status, (body or b"")[:200]))

    data = json.loads(body)
    outgoing = data.get("Outgoing") or []

    per_dest_totals = {}
    for conn in outgoing:
        dest = conn.get("Destination") or "<unknown>"
        if destination_filter and destination_filter not in dest:
            continue
        total = 0
        for perf in (conn.get("Performance") or []):
            net = perf.get("Network") or {}
            v = net.get(field)
            if isinstance(v, int):
                total += v
        per_dest_totals[dest] = per_dest_totals.get(dest, 0) + total

    current = sum(per_dest_totals.values())

    lines = ["replication_outgoing_count  target=%s  field=%s%s" %
             (target, field,
              ("  destination_filter=%s" % destination_filter) if destination_filter else "")]
    lines.append("  per-destination current totals:")
    if per_dest_totals:
        width = max(len(d) for d in per_dest_totals)
        for dest in sorted(per_dest_totals):
            lines.append("    %-*s  %d" % (width, dest, per_dest_totals[dest]))
    else:
        lines.append("    (no matching outgoing connections)")
    lines.append("  current total: %d" % current)

    if snapshot_path and not os.path.exists(snapshot_path):
        with open(snapshot_path, "w") as f:
            json.dump({"field": field,
                       "destination_filter": destination_filter,
                       "total": current,
                       "per_destination": per_dest_totals}, f)
        lines.append("CAPTURED  baseline written to %s (total=%d)" % (snapshot_path, current))
        return lines

    if snapshot_path:
        with open(snapshot_path, "r") as f:
            baseline = json.load(f)
        if baseline.get("field") != field:
            raise RuntimeError("snapshot field mismatch: baseline=%r, current=%r" %
                               (baseline.get("field"), field))
        baseline_total = int(baseline.get("total") or 0)
        delta = current - baseline_total
        lines.append("  baseline total (from %s): %d" % (snapshot_path, baseline_total))
        lines.append("  DELTA: %d" % delta)
    else:
        delta = current

    assert_max = p["assert_max"]
    assert_min = p["assert_min"]
    assert_exact = p["assert_exact"]

    if assert_exact is not None:
        if delta != int(assert_exact):
            return violation(assert_mode, lines +
                             ["FAIL  expected delta == %d but got %d" % (int(assert_exact), delta)])
        lines.append("PASS  delta == %d" % int(assert_exact))
        return lines

    bound_set = False
    if assert_max is not None:
        bound_set = True
        if delta > int(assert_max):
            return violation(assert_mode, lines +
                             ["FAIL  expected delta <= %d but got %d" % (int(assert_max), delta)])
    if assert_min is not None:
        bound_set = True
        if delta < int(assert_min):
            return violation(assert_mode, lines +
                             ["FAIL  expected delta >= %d but got %d" % (int(assert_min), delta)])

    if bound_set:
        lines.append("PASS  delta within bounds (min=%s, max=%s)" %
                     (assert_min, assert_max))
    return lines


def k_schema_version(p):
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

    if p["db_name"]:
        leader = p["cluster_leader"] or nodes[0]
        rec_path = "/admin/databases?name=%s" % quote(p["db_name"])
        status, body = request("GET", leader, p["ravendb_domain"], rec_path,
                               p["client_cert"], p["ca_cert"])
        if status == 200:
            features = json.loads(body).get("SupportedFeatures") or {}
            lines.append("DatabaseRecord.SupportedFeatures (via %s): %s" % (leader, features))

    distinct = set(version_map.values())
    if require_parity and assert_mode and len(distinct) > 1:
        return violation(True, lines + ["FAIL  version mismatch across nodes"])

    if expected and assert_mode:
        bad = [n for n, v in version_map.items() if expected not in v]
        if bad:
            return violation(True, lines + ["FAIL  nodes missing expected '%s': %s" % (expected, bad)])

    return lines


def k_size_envelope(p):
    nodes = p["nodes"]
    baseline_file = p["baseline_file"]
    max_growth_pct = float(p["max_growth_pct"] or 300)
    assert_mode = bool(p["assert_mode"])

    sizes = {}
    for target in nodes:
        stats = get_stats(p, target)
        if stats:
            sizes[target] = int((stats.get("SizeOnDisk") or {}).get("SizeInBytes") or 0)

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
    for target in nodes:
        base = int(baseline.get(target, 0))
        curr = sizes.get(target, 0)
        pct = ((curr - base) * 100.0 / base) if base > 0 else 0.0
        growths[target] = pct
        lines.append("  %-6s  baseline=%d  current=%d  growth=%+.1f%%" % (target, base, curr, pct))

    over = [n for n, pct in growths.items() if pct > max_growth_pct]
    if over and assert_mode:
        return violation(True, lines + ["FAIL  growth > %.1f%% on %s" % (max_growth_pct, over)])

    if over:
        lines.append("INFO  growth exceeds %.1f%% on %s (assert_mode=false; not failing)" % (
            max_growth_pct, over))
    return lines


def k_capture_cv(p):
    nodes = p["nodes"]
    db = p["db_name"]
    output_dir = p["output_dir"]
    if not output_dir:
        raise ValueError("kind=capture_cv requires `output_dir` (wrapper YAML resolves the default)")

    os.makedirs(output_dir, exist_ok=True)

    written = []
    for target in nodes:
        stats = get_stats(p, target)
        cv = (stats or {}).get("DatabaseChangeVector") or "<UNAVAILABLE>"
        path = os.path.join(output_dir, "%s.cv" % target)
        with open(path, "w") as f:
            f.write(cv + "\n")
        written.append(target)

    return "captured DatabaseChangeVector for %d node(s) -> %s/" % (len(written), output_dir)


def k_capture_doc_cv(p):
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
    for target in nodes:
        for doc_id in ids:
            path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
            status, body = request("GET", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
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

    return "captured %d per-(node,id) CV file(s) -> %s/" % (pairs, output_dir)


def k_scan_fltr(p):
    capture_dir = p["capture_dir"]
    assert_mode = bool(p["assert_mode"])
    if not capture_dir or not os.path.isdir(capture_dir):
        raise ValueError("capture_dir must be an existing directory; got %r" % capture_dir)

    found_files = []
    for dirpath, _dirs, files in os.walk(capture_dir):
        for name in files:
            if name.endswith(".cv"):
                found_files.append(os.path.join(dirpath, name))

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


def k_doc_count_parity(p):
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
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    probe_ids = expand_id_set(p)

    def probe_all(ids):
        # Returns {id: [status_per_node]}.
        out = {}
        for doc_id in ids:
            statuses = []
            for target in nodes:
                path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
                status, _ = request("GET", target, p["ravendb_domain"], path,
                                    p["client_cert"], p["ca_cert"])
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

    settle_retries = 3
    settle_secs = 2
    for _ in range(settle_retries):
        if not mismatched:
            break
        time.sleep(settle_secs)
        retry_status = probe_all(mismatched)
        id_status.update(retry_status)
        mismatched = split_ids({i: id_status[i] for i in mismatched})

    lines = ["doc-id-set parity: %d id(s) probed across %s" % (len(probe_ids), nodes),
             "  mismatched: %d" % len(mismatched)]
    if mismatched:
        lines.append("  mismatched ids (first 20):")
        for i in mismatched[:20]:
            lines.append("    %-30s  per-node statuses=%s" % (i, id_status[i]))
    if mismatched:
        return violation(assert_mode, lines + ["FAIL  doc-id-set split (persisted across %d retries x %ds settle)" %
                                               (settle_retries, settle_secs)])
    lines.append("PASS  every probe id is uniformly present or uniformly absent")
    return lines


def k_revision_count_parity(p):
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    expected = p["expected_count"]
    page_size = int(p["page_size"] or 1024)
    probe_ids = expand_id_set(p)

    id_counts = {}
    for doc_id in probe_ids:
        counts = []
        for target in nodes:
            path = "/databases/%s/revisions?id=%s&pageSize=%d" % (db, quote(doc_id), page_size)
            status, body = request("GET", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
            if status == 200:
                counts.append(len(json.loads(body).get("Results") or []))
            else:
                counts.append(None)
        id_counts[doc_id] = counts

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


def k_orphan_revisions(p):
    nodes = p["nodes"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    budget = int(p["budget_secs"] or 60)

    # Trigger adopt on every node.
    op_ids = {}
    for target in nodes:
        path = "/databases/%s/admin/revisions/orphaned/adopt" % db
        status, body = request("POST", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"], body={})
        if status not in (200, 201):
            raise RuntimeError("trigger adopt on %s failed: HTTP %d" % (target, status))
        op_ids[target] = int(json.loads(body)["OperationId"])

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
                adopted[target] = "?"
                inconclusive.append(target)
        for t in done_now:
            pending.pop(t, None)
        if pending:
            time.sleep(2)

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


def k_extension_stats_parity(p):
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


def k_stored_item_cv_split(p):
    target = p["target"]
    db = p["db_name"]
    doc_ids = p["doc_ids"]
    expect = p["expect"] or "split"
    delimiter = p["delimiter"] or "|"
    assert_mode = bool(p["assert_mode"])

    if not doc_ids:
        raise ValueError("kind=stored_item_cv_split requires `doc_ids` (non-empty list)")

    violations = []
    lines = ["stored-item CV shape check on %s/%s (expect=%s, delimiter='%s'):" %
             (target, db, expect, delimiter)]

    cvs = {}
    for doc_id in doc_ids:
        path = "/databases/%s/docs?id=%s" % (db, quote(doc_id))
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            lines.append("  %s  HTTP %d (skipped)" % (doc_id, status))
            continue
        results = json.loads(body).get("Results") or []
        cv = ""
        if results:
            cv = (results[0].get("@metadata") or {}).get("@change-vector") or ""
        cvs[doc_id] = cv

    if not cvs:
        return violation(assert_mode, lines +
                         ["FAIL  every probe doc was unreadable (%d/%d HTTP != 200) -- "
                          "check that doc_ids actually exist on %s" %
                          (len(doc_ids), len(doc_ids), target)])

    if expect == "split" and not any(delimiter in v for v in cvs.values()):
        for doc_id, cv in cvs.items():
            lines.append("  %s  LEGACY raw CV (no '%s')" % (doc_id, delimiter))
        lines.append("N/A  legacy raw-CV form across all probes -- composite-CV lane not active on this build")
        return lines

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


def k_lane_inert(p):
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
    for target in nodes:
        for doc_id in probe_ids:
            path = "/databases/%s/revisions?id=%s&pageSize=100" % (db, quote(doc_id))
            status, body = request("GET", target, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
            if status != 200:
                continue
            revisions = json.loads(body).get("Results") or []
            sampled += len(revisions)
            for rev in revisions:
                cv = (rev.get("@metadata") or {}).get("@change-vector") or ""
                if "|" in cv:
                    leaks.append("%s / %s / %s" % (target, doc_id, cv))

    rec_path = "/admin/databases?name=%s" % quote(db)
    status, body = request("GET", nodes[0], p["ravendb_domain"], rec_path,
                           p["client_cert"], p["ca_cert"])
    features = {}
    if status == 200:
        features = json.loads(body).get("SupportedFeatures") or {}

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
    sink_leader = p["sink_cluster_leader"]
    db = p["db_name"]
    assert_mode = bool(p["assert_mode"])
    allowed = p["allowed_prefixes"]

    if not allowed:
        rec_path = "/admin/databases?name=%s" % quote(db)
        status, body = request("GET", sink_leader, p["ravendb_domain"], rec_path,
                               p["client_cert"], p["ca_cert"])
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
    status, body = request("GET", sink_leader, p["ravendb_domain"], list_path,
                           p["client_cert"], p["ca_cert"])
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
        status, _ = request("GET", sink_leader, p["ravendb_domain"], path,
                            p["client_cert"], p["ca_cert"])
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


_CV_DBID_RE = re.compile(r"([A-Za-z0-9]+):\d+-([A-Za-z0-9+/=_\-]+)")


def parse_cv_entries(cv):
    """Return list of (tag, dbid) tuples extracted from a CV string."""
    return _CV_DBID_RE.findall(cv or "")


def k_cv_boundary_by_dbid(p):
    db = p["db_name"]
    sources = p["source_nodes"]
    receivers = p["receiver_nodes"]
    strict_v_new = bool(p["strict_v_new"])
    assert_mode = bool(p["assert_mode"])

    if not sources or not receivers:
        raise ValueError("kind=cv_boundary_by_dbid requires `source_nodes` and `receiver_nodes`")

    source_dbids = set()
    for target in sources:
        s = get_stats(p, target)
        dbid = (s or {}).get("DatabaseId")
        if dbid:
            source_dbids.add(dbid)

    receiver_dbids = set()
    receiver_cvs = {}
    for target in receivers:
        s = get_stats(p, target)
        if s is None:
            continue
        dbid = s.get("DatabaseId")
        if dbid:
            receiver_dbids.add(dbid)
        receiver_cvs[target] = s.get("DatabaseChangeVector") or ""

    if not source_dbids or not receiver_dbids or (source_dbids & receiver_dbids):
        raise RuntimeError(
            "could not establish disjoint dbid sets; source=%s receiver=%s" %
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
    for target in receivers:
        s = get_stats(p, target)
        cv = (s or {}).get("DatabaseChangeVector") or ""
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

    if offending:
        return violation(assert_mode, lines +
                         ["FAIL  CV-boundary breach: foreign tag on: %s" % offending])
    lines.append("PASS  every receiver's DB CV references only %s" % tags)
    return lines


_CV_ENTRY_FULL_RE = re.compile(r"([A-Za-z0-9]+):(\d+)-([A-Za-z0-9+/=_\-]+)")


def k_cross_cluster_cv_equality(p):
    nodes = p["nodes"]
    db = p["db_name"]
    doc_ids = p["doc_ids"]
    assert_mode = bool(p["assert_mode"])

    if not nodes or len(nodes) < 2:
        raise ValueError("kind=cross_cluster_cv_equality requires `nodes` with >=2 targets")
    if not doc_ids:
        raise ValueError("kind=cross_cluster_cv_equality requires `doc_ids` (non-empty list)")

    def _parse_cv_set(cv_str):
        if not cv_str:
            return frozenset()
        out = set()
        for _tag, etag, dbid in _CV_ENTRY_FULL_RE.findall(cv_str):
            try:
                out.add((dbid, int(etag)))
            except ValueError:
                pass
        return frozenset(out)

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

        # Compare frozensets across all nodes.
        parsed_sets = [per_node[t][1] for t in nodes]
        if any(s != parsed_sets[0] for s in parsed_sets[1:]):
            mismatched.append(doc_id)
            lines.append("  %-25s  MISMATCH" % doc_id)
            for t in nodes:
                cv, parsed = per_node[t]
                lines.append("    %s  cv='%s'  entries=%s" %
                             (t, (cv or "")[:80], sorted(parsed) if parsed else "<empty>"))

    lines.append("  checked %d doc(s); mismatched=%d  unreachable=%d" %
                 (len(doc_ids), len(mismatched), len(unreachable)))

    if mismatched or unreachable:
        return violation(assert_mode, lines + [
            "FAIL  cross-cluster CV equality broke on %d doc(s); unreachable on %d" %
            (len(mismatched), len(unreachable))])
    if assert_mode:
        lines.append("PASS  every doc has the same CV-entry set across all %d cluster(s)" % len(nodes))
    return lines


_STATS_ALL_FIELDS = [
    "CountOfAttachments", "CountOfConflicts", "CountOfCounterEntries",
    "CountOfDocuments", "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTimeSeriesSegments", "CountOfTombstones", "CountOfUniqueAttachments",
]
_STATS_DEFAULT_ASSERTED = [
    "CountOfAttachments", "CountOfConflicts", "CountOfDocuments",
    "CountOfDocumentsConflicts", "CountOfRemoteAttachments",
    "CountOfRevisionDocuments", "CountOfTimeSeriesDeletedRanges",
    "CountOfTombstones", "CountOfUniqueAttachments",
]


def k_stats_parity(p):
    nodes = p["nodes"]
    assert_mode = bool(p["assert_mode"])
    assert_fields = p["assert_fields"] or _STATS_DEFAULT_ASSERTED

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

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

    node_widths = {}
    for n in node_order:
        widest = max(len(str(per_node[n][f])) for f in _STATS_ALL_FIELDS + ["SizeOnDisk"])
        node_widths[n] = max(widest, len(n), 8)
    field_width = max(len(f) for f in _STATS_ALL_FIELDS + ["SizeOnDisk (info only)"])

    lines = ["/stats parity  db=%s  nodes=%s" % (p["db_name"], node_order)]
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



KINDS = {
    # info-only
    "doc_count":              k_doc_count,
    "replication":            k_replication,
    "replication_outgoing_count": k_replication_outgoing_count,
    "schema_version":         k_schema_version,
    "size_envelope":          k_size_envelope,
    "capture_cv":             k_capture_cv,
    "capture_doc_cv":         k_capture_doc_cv,
    "scan_fltr":              k_scan_fltr,
    "doc_count_parity":       k_doc_count_parity,
    "doc_id_set_parity":      k_doc_id_set_parity,
    "revision_count_parity":  k_revision_count_parity,
    "orphan_revisions":       k_orphan_revisions,
    "extension_stats_parity": k_extension_stats_parity,
    "stored_item_cv_split":   k_stored_item_cv_split,
    "lane_inert":             k_lane_inert,
    "filter_compliance":      k_filter_compliance,
    "cross_sink_isolation":   k_cross_sink_isolation,
    "cv_boundary_by_dbid":    k_cv_boundary_by_dbid,
    "db_cv_order_side_only":  k_db_cv_order_side_only,
    "cross_cluster_cv_equality": k_cross_cluster_cv_equality,
    "stats_parity":           k_stats_parity,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
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
        # id sets
        ids=dict(type="list", elements="str", default=None),
        id_prefix=dict(default=None),
        count=dict(type="int", default=None),
        doc_ids=dict(type="list", elements="str", default=None),
        id_prefixes=dict(type="list", elements="str", default=None),
        sample_per_prefix=dict(type="int", default=None),
        # schema_version
        require_parity=dict(type="bool", default=False),
        expected_version=dict(default=None),
        # size_envelope
        baseline_file=dict(type="path", default=None),
        max_growth_pct=dict(type="float", default=None),
        # capture_*
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
        # stored_item_cv_split
        expect=dict(default=None, choices=["raw", "split", None]),
        delimiter=dict(default=None),
        # filter_compliance / cross_sink_isolation
        sink_cluster_leader=dict(default=None),
        allowed_prefixes=dict(type="list", elements="str", default=None),
        forbidden_prefixes=dict(type="list", elements="str", default=None),
        # cv_boundary_by_dbid
        source_nodes=dict(type="list", elements="str", default=None),
        receiver_nodes=dict(type="list", elements="str", default=None),
        strict_v_new=dict(type="bool", default=False),
        # db_cv_order_side_only
        receiver_group_nodes=dict(type="list", elements="str", default=None),
        receiver_group_tags=dict(type="list", elements="str", default=None),
        # stats_parity
        assert_fields=dict(type="list", elements="str", default=None),
        # replication_outgoing_count
        counter_kind=dict(default=None,
                          choices=["document", "revision", "attachment", "counter", "time_series", None]),
        snapshot_path=dict(type="path", default=None),
        destination_filter=dict(default=None),
        assert_max=dict(type="int", default=None),
        assert_min=dict(type="int", default=None),
        assert_exact=dict(type="int", default=None),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except DiagnosticViolation as e:
        module.fail_json(msg=e.lines)
    except Exception as e:
        module.fail_json(msg=str(e))

    module.exit_json(changed=False, msg=message)


if __name__ == "__main__":
    main()
