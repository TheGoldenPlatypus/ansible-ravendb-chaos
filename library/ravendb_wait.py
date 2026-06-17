#!/usr/bin/python

import json
import random
import time
from urllib.parse import quote

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.ravendb_client import (
    orchestrator_for,
    prefix_match,
    request,
    request_per_node,
    stream_all_doc_ids,
    with_node_tag,
)
from ansible.module_utils.polling import poll_until


def format_per_node(per_node, indent="  "):
    if not per_node:
        return [indent + "(empty)"]
    width = max(len(name) for name in per_node)
    lines = []
    for name in sorted(per_node):
        lines.append("%s%-*s  %s" % (indent, width, name, per_node[name]))
    return lines


def now_hms():
    return time.strftime("%H:%M:%S")


def classify_nodes(p, nodes):
    """Classify each probe node by whether it hosts the database.

    Strategy:
      1. Look up the database's routing once via orchestrator_for():
         non-sharded -> probe each node directly at /databases/<db>/stats
         sharded     -> route every probe through the orchestrator with
                        ?nodeTag=<X>; the orchestrator proxies to the
                        named node.

    Returns (host_map, skipped) where:
      host_map[target] = None              for non-sharded reachable nodes
      host_map[target] = ('via', orch_tgt) for sharded reachable nodes; the
                                            tuple says "to read stats from
                                            this node, send GET via orch_tgt
                                            with ?nodeTag=<target tag>".
      skipped: list of nodes that returned a non-200, non-410 response
                (the db genuinely isn't there -- 404 etc).

    Raises RuntimeError on:
      - unreachable nodes (connection error, DNS, SSL)
      - failed orchestrator lookup
    -- because conflating those with "wrong cluster" is the root cause of
    vacuous waits passing on dead clusters."""
    domain = p["ravendb_domain"]
    db = p["db_name"]
    if not nodes:
        return {}, []

    # One lookup against the first node to determine the orchestrator.  If
    # the db is sharded, every subsequent probe routes via that orchestrator.
    # Defensive: if the lookup itself fails (transport / DNS / permission),
    # default to non-sharded and let the per-node probe below surface the
    # failure with the original ("X: %s" % body) error context -- callers
    # rely on that shape for the "unreachable" assertion.
    try:
        orch = orchestrator_for(nodes[0], domain, db,
                                p["client_cert"], p["ca_cert"])
        is_sharded = (orch != nodes[0]) or _is_sharded(p, nodes[0])
    except Exception:
        orch = nodes[0]
        is_sharded = False

    host_map = {}
    skipped = []
    unreachable = []

    if not is_sharded:
        path = "/databases/%s/stats" % db
        results = request_per_node("GET", nodes, domain, path,
                                   p["client_cert"], p["ca_cert"])
        for target, status, body in results:
            if status is None:
                unreachable.append("%s: %s" % (target, body))
            elif status == 200:
                host_map[target] = None
            else:
                skipped.append(target)
    else:
        # Sharded: per-node calls go to the orchestrator with ?nodeTag=<tag>.
        # Each probe verifies the proxy reaches the named node successfully.
        base_path = "/databases/%s/stats" % db
        for target in nodes:
            tag = target[-1].upper()
            path = with_node_tag(base_path, tag)
            try:
                s, _ = request("GET", orch, domain, path,
                               p["client_cert"], p["ca_cert"])
            except Exception as e:
                unreachable.append("%s (via orch %s): %r" % (target, orch, e))
                continue
            if s == 200:
                host_map[target] = ("via", orch)
            else:
                skipped.append(target)

    if unreachable:
        raise RuntimeError(
            "classify_nodes: %d/%d probe node(s) unreachable: %s -- "
            "can't proceed with a wait on partial coverage (vacuous PASS risk)"
            % (len(unreachable), len(nodes), unreachable))

    return host_map, skipped


def stats_request_for(target, host_map_value, db, suffix="stats"):
    """Compose (call_target, url_path) for a /stats-family call against
    `target` using the routing decision classify_nodes already made.

    host_map_value is what classify_nodes stored:
        None              -> non-sharded, call /databases/<db>/<suffix> on target
        ("via", orch_tgt) -> sharded, call /databases/<db>/<suffix>?nodeTag=<X>
                             on orch_tgt where X = target's cluster tag

    Callers send the resulting (call_target, path) via request() instead of
    hand-rolling the URL composition (which previously had per-shard logic)."""
    base = "/databases/%s/%s" % (db, suffix)
    if host_map_value is None:
        return target, base
    _, orch_target = host_map_value
    tag = target[-1].upper()
    return orch_target, with_node_tag(base, tag)


def _is_sharded(p, target):
    """Best-effort: tell whether `target`'s DB record reports sharding.
    Used as a tie-break when orchestrator_for returned target unchanged
    (which can happen for non-sharded OR for sharded-where-target-is-the-
    orchestrator -- both legitimate)."""
    s, b = request("GET", target, p["ravendb_domain"],
                   "/admin/databases?name=%s" % p["db_name"],
                   p["client_cert"], p["ca_cert"])
    if s != 200:
        return False
    shards = ((json.loads(b).get("Sharding") or {}).get("Shards")) or {}
    return bool(shards)

    return host_map, skipped


def k_leader(p):
    target = p["target"]
    timeout = p["timeout"]
    expected = (p["expected_leader"] or "").upper()
    path = "/cluster/topology"

    def predicate():
        status, body = request("GET", target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        leader = json.loads(body).get("Leader") or ""
        if not leader:
            return False, "no leader yet"
        if expected and expected not in leader:
            return False, "leader='%s' (waiting for tag '%s')" % (leader, expected)
        return True, leader

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError("TIMEOUT after %.1fs -- %s" % (elapsed, value))

    if expected:
        return ("LEADER elected -- '%s' contains expected tag '%s' "
                "(queried via %s, took %.1fs)" % (value, expected, target, elapsed))
    return "LEADER elected -- '%s' (queried via %s, took %.1fs)" % (
        value, target, elapsed)


def k_member(p):
    cluster_leader = p["cluster_leader"]
    db = p["db_name"]
    target = p["target"]
    target_tag = (p["target_tag"] or target[-1]).upper()
    timeout = p["timeout"]
    path = "/admin/databases?name=%s" % db

    def gather(record):
        sharding = record.get("Sharding") or {}
        shards = sharding.get("Shards") or {}
        if shards:
            orch = (sharding.get("Orchestrator") or {}).get("Topology") or {}
            members_seen = set()
            promotables_seen = set()
            rehabs_seen = set()
            for entry in (orch,) + tuple(shards.values()):
                members_seen.update(entry.get("Members") or [])
                promotables_seen.update(entry.get("Promotables") or [])
                rehabs_seen.update(entry.get("Rehabs") or [])
            return (sorted(members_seen),
                    sorted(promotables_seen),
                    sorted(rehabs_seen),
                    "sharded(orch+%d shards)" % len(shards))

        flat = record.get("Topology") or {}
        return (list(flat.get("Members") or []),
                list(flat.get("Promotables") or []),
                list(flat.get("Rehabs") or []),
                "flat")

    def predicate():
        status, body = request("GET", cluster_leader, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        record = json.loads(body)
        members, promotables, rehabs, kind = gather(record)
        ready = (target_tag in members
                 and target_tag not in promotables
                 and target_tag not in rehabs)
        if ready:
            return True, "[%s] Members=%s" % (kind, members)
        return False, ("[%s] Members=%s Promotables=%s Rehabs=%s" %
                       (kind, members, promotables, rehabs))

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- tag '%s' never became Member; last: %s" %
            (elapsed, target_tag, value))

    return ("REHAB COMPLETE -- node %s (tag %s) is full Member of %s "
            "(via %s, took %.1fs; %s)" % (target, target_tag, db, cluster_leader, elapsed, value))


def k_rehab(p):
    cluster_leader = p["cluster_leader"]
    db = p["db_name"]
    target = p["target"]
    target_tag = (p["target_tag"] or target[-1]).upper()
    timeout = p["timeout"]
    path = "/admin/databases?name=%s" % db

    def predicate():
        status, body = request("GET", cluster_leader, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
        if status != 200:
            return False, "HTTP %d" % status
        topo = json.loads(body).get("Topology") or {}
        promotables = topo.get("Promotables") or []
        rehabs = topo.get("Rehabs") or []
        if target_tag in promotables or target_tag in rehabs:
            return True, "Promotables=%s Rehabs=%s" % (promotables, rehabs)
        members = topo.get("Members") or []
        return False, "still Member; Members=%s" % members

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- tag '%s' never entered rehab "
            "(chaos action may have failed to destabilise the node); last: %s" %
            (elapsed, target_tag, value))

    return ("REHAB STARTED -- node %s (tag %s) entered Promotables/Rehabs on %s "
            "(via %s, took %.1fs)" % (target, target_tag, db, cluster_leader, elapsed))


def snapshot_stats_field(p, nodes_or_map, field):
    """Sample `field` from /stats on every node in nodes_or_map.

    HARDENED: every probed node must respond 200, and the field must be
    present in the response.  Any HTTP failure, connection error, or missing
    field raises RuntimeError -- silently dropping failed nodes from the
    snapshot is the bug that lets `all(prev.get(n) == current.get(n) for n in
    has_db)` become vacuously True on `snap == {}` and pass a wait against a
    dead cluster.

    Returns a dict {target: field_value} where field_value may be a value the
    server returned (including legitimate None if the field is actually null
    -- but field absence is reported separately as a failure).
    """
    db = p["db_name"]
    snap = {}
    failures = []

    if isinstance(nodes_or_map, dict):
        for target, route_marker in nodes_or_map.items():
            call_target, path = stats_request_for(target, route_marker, db)
            try:
                s, b = request("GET", call_target, p["ravendb_domain"], path,
                               p["client_cert"], p["ca_cert"])
            except Exception as e:
                failures.append("%s: connection error %r" % (target, e))
                continue
            if s != 200:
                failures.append("%s: HTTP %d" % (target, s))
                continue
            data = json.loads(b)
            if field not in data:
                failures.append("%s: response has no '%s' field" % (target, field))
                continue
            snap[target] = data[field]
    else:
        # legacy plain-list path -- non-sharded only
        path = "/databases/%s/stats" % db
        results = request_per_node("GET", nodes_or_map, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
        for target, status, body in results:
            if status is None:
                failures.append("%s: connection error %s" % (target, body))
                continue
            if status != 200:
                failures.append("%s: HTTP %d" % (target, status))
                continue
            data = json.loads(body)
            if field not in data:
                failures.append("%s: response has no '%s' field" % (target, field))
                continue
            snap[target] = data[field]

    if failures:
        raise RuntimeError(
            "snapshot_stats_field('%s') failed on %d/%d node(s): %s -- "
            "can't proceed with a wait on partial coverage (would silently "
            "PASS via all([])==True against a dead cluster)"
            % (field, len(failures), len(nodes_or_map), failures))

    if not snap:
        # Defensive: nodes_or_map was empty AND no failures.  The classify_nodes
        # guard upstream should already block this, but never trust.
        raise RuntimeError(
            "snapshot_stats_field('%s'): 0 nodes contributed data -- "
            "vacuous PASS would be a silent false negative" % field)

    return snap


def k_etag_parity(p):
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    prev = None
    prev_time = None

    def predicate():
        nonlocal prev, prev_time
        current = snapshot_stats_field(p, has_db, "LastDatabaseEtag")
        current_time = now_hms()
        if not has_db:
            # defense in depth: classify_nodes already raises if has_db is
            # empty, but if any future caller bypasses that, refuse to PASS
            # via all([])==True.
            raise RuntimeError("k_etag_parity: has_db is empty -- vacuous PASS guard")
        if prev is None:
            prev = current
            prev_time = current_time
            return False, (current_time, current, current_time, current)
        stable = all(prev.get(n) == current.get(n) for n in has_db)
        snapshots = (prev_time, prev, current_time, current)
        if stable:
            return True, snapshots
        prev = current
        prev_time = current_time
        return False, snapshots

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    t1, snap1, t2, snap2 = value

    if not done:
        lines = ["TIMEOUT after %.1fs -- LastDatabaseEtag still moving:" % elapsed]
        lines.append("  sample 1 (%s):" % t1)
        lines.extend(format_per_node(snap1, indent="    "))
        lines.append("  sample 2 (%s):" % t2)
        lines.extend(format_per_node(snap2, indent="    "))
        raise RuntimeError("\n".join(lines))

    header = ("STABLE -- LastDatabaseEtag unchanged across %d node(s) "
              "(skipped=%s, took %.1fs)" % (len(has_db), skipped, elapsed))
    lines = [header, "  sample 1 (%s):" % t1]
    lines.extend(format_per_node(snap1, indent="    "))
    lines.append("  sample 2 (%s):" % t2)
    lines.extend(format_per_node(snap2, indent="    "))
    return lines


def k_docs_drain(p):
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    prev = None
    prev_time = None

    def predicate():
        nonlocal prev, prev_time
        current = snapshot_stats_field(p, has_db, "DatabaseChangeVector")
        current_time = now_hms()
        if not has_db:
            raise RuntimeError("k_docs_drain: has_db is empty -- vacuous PASS guard")
        if prev is None:
            prev = current
            prev_time = current_time
            return False, (current_time, current, current_time, current)
        stable = all(prev.get(n) == current.get(n) for n in has_db)
        snapshots = (prev_time, prev, current_time, current)
        if stable:
            return True, snapshots
        prev = current
        prev_time = current_time
        return False, snapshots

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    t1, snap1, t2, snap2 = value

    if not done:
        lines = ["TIMEOUT after %.1fs -- DatabaseChangeVector still moving:" % elapsed]
        lines.append("  sample 1 (%s):" % t1)
        lines.extend(format_per_node(snap1, indent="    "))
        lines.append("  sample 2 (%s):" % t2)
        lines.extend(format_per_node(snap2, indent="    "))
        raise RuntimeError("\n".join(lines))

    header = ("DRAINED -- DatabaseChangeVector unchanged across %d node(s) "
              "(skipped=%s, took %.1fs)" % (len(has_db), skipped, elapsed))
    lines = [header, "  sample 1 (%s):" % t1]
    lines.extend(format_per_node(snap1, indent="    "))
    lines.append("  sample 2 (%s):" % t2)
    lines.extend(format_per_node(snap2, indent="    "))
    return lines


def k_quiescence(p):
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    def predicate():
        current = snapshot_stats_field(p, has_db, "DatabaseChangeVector")
        if not current:
            raise RuntimeError("k_quiescence: snapshot is empty -- vacuous PASS guard")
        # Reject 'all None / all empty string' as a false convergence: that
        # means every node returned a missing/empty CV, not that they agree.
        if all((v is None or v == "") for v in current.values()):
            raise RuntimeError(
                "k_quiescence: every node returned a missing/empty "
                "DatabaseChangeVector %s -- can't claim convergence on "
                "empty data" % current)
        unique = set(current.values())
        if len(unique) == 1:
            return True, current
        return False, current

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- DatabaseChangeVectors never converged:\n%s" %
            (elapsed, "\n".join(format_per_node(value))))

    agreed = next(iter(set(value.values())))
    return [
        "QUIESCENT -- %d node(s) agree on DatabaseChangeVector "
        "(skipped=%s, took %.1fs):" % (len(has_db), skipped, elapsed),
        "  " + str(agreed),
    ]


def k_stats_field_parity(p):
    nodes = p["nodes"]
    fields = p["fields"] or ["CountOfTombstones"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    def predicate():
        per_field = {}
        for field in fields:
            per_field[field] = snapshot_stats_field(p, has_db, field)
            if not per_field[field]:
                raise RuntimeError(
                    "k_stats_field_parity: snapshot for '%s' is empty -- "
                    "vacuous PASS guard" % field)

        drift = []
        for field in fields:
            values = set(per_field[field].values())
            if len(values) > 1:
                drift.append("%s: %s" % (field, per_field[field]))

        if not drift:
            return True, per_field
        return False, drift

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        drift_lines = "\n".join("  " + line for line in value)
        raise RuntimeError(
            "TIMEOUT after %.1fs -- field(s) never converged:\n%s" % (elapsed, drift_lines))

    summary_lines = []
    for field in fields:
        agreed = next(iter(set(value[field].values())))
        summary_lines.append("  %s = %s" % (field, agreed))
    header = ("PARITY -- fields %s match across %d node(s) "
              "(skipped=%s, took %.1fs):" % (fields, len(has_db), skipped, elapsed))
    return [header] + summary_lines


def k_conflicts_resolved(p):
    nodes = p["nodes"]
    timeout = p["timeout"]
    interval = p["poll_interval"]

    has_db, skipped = classify_nodes(p, nodes)
    if not has_db:
        raise ValueError("no probed node has database '%s'" % p["db_name"])

    path = "/databases/%s/replication/conflicts" % p["db_name"]

    def predicate():
        targets = list(has_db.keys()) if isinstance(has_db, dict) else has_db
        results = request_per_node("GET", targets, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
        counts = {}
        failures = []
        for target, status, body in results:
            if status is None:
                failures.append("%s: connection error %s" % (target, body))
                continue
            if status != 200:
                failures.append("%s: HTTP %d" % (target, status))
                continue
            data = json.loads(body)
            counts[target] = data.get("TotalResults", len(data.get("Results") or []))

        if failures:
            # Don't pretend a network error is 'conflicts still present' --
            # that masks unreachable nodes as 'not converged' and burns the
            # entire timeout budget.  Raise loud immediately.
            raise RuntimeError(
                "k_conflicts_resolved: %d/%d node(s) failed during conflict "
                "probe: %s -- can't verify resolution on partial coverage"
                % (len(failures), len(targets), failures))

        if not counts:
            raise RuntimeError(
                "k_conflicts_resolved: 0 nodes contributed conflict counts -- "
                "vacuous PASS guard")

        all_zero = all(v == 0 for v in counts.values())
        if all_zero:
            return True, counts
        return False, counts

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- conflicts never resolved; last: %s" %
            (elapsed, value))

    return ("CONFLICTS RESOLVED -- every node reports zero conflicts: %s "
            "(skipped=%s, took %.1fs)" % (value, skipped, elapsed))


def k_marker_propagation(p):
    source = p["source"]
    targets = p["targets"]
    db = p["db_name"]
    timeout = p["timeout"]
    prefix = p["marker_id_prefix"] or "markers/"

    marker_id = "%s%d-%d" % (prefix, time.time_ns(), random.randint(0, 99999))

    put_path = "/databases/%s/docs?id=%s" % (db, quote(marker_id))
    put_body = {"@metadata": {"@collection": "Markers"}}
    status, _ = request("PUT", source, p["ravendb_domain"], put_path,
                        p["client_cert"], p["ca_cert"], body=put_body)
    if status not in (200, 201):
        raise RuntimeError("failed to PUT marker on %s: HTTP %d" % (source, status))

    get_path = "/databases/%s/docs?id=%s" % (db, quote(marker_id))
    pending = set(targets)

    def predicate():
        if not pending:
            return True, "all targets received"
        results = request_per_node("GET", list(pending), p["ravendb_domain"], get_path,
                                   p["client_cert"], p["ca_cert"])
        unreachable = []
        for target, status, body in results:
            if status is None:
                # Connection error.  We're polling, so it MIGHT recover, but
                # if every retry sees the same network failure we'd spin to
                # timeout and misattribute the cause as 'marker did not
                # propagate'.  Raise loud now -- a wait kind running against
                # a dead target is meaningless.
                unreachable.append("%s: %s" % (target, body))
                continue
            if status == 200:
                pending.discard(target)
        if unreachable:
            raise RuntimeError(
                "k_marker_propagation: %d target(s) unreachable: %s -- "
                "can't verify marker propagation to dead nodes"
                % (len(unreachable), unreachable))
        if not pending:
            return True, "all targets received"
        return False, "still waiting on %s" % sorted(pending)

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=2.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- marker '%s' did not propagate; %s" %
            (elapsed, marker_id, value))

    return ("MARKER PROPAGATED -- '%s' reached every target %s from %s "
            "(took %.1fs)" % (marker_id, targets, source, elapsed))


def k_doc_count_match(p):
    src_target = p["source"]
    src_db     = p["db_name"]
    tgt_target = p["target"]
    tgt_db     = p["target_db_name"] or src_db
    timeout    = p["timeout"]

    def _aggregate_count(target, db):
        """Return CountOfDocuments for `db` on the cluster reachable via
        `target`.  Sharded DBs use /stats/essential on the orchestrator
        (server-side fan-out + aggregation); non-sharded use /stats.

        Step 1 looks up the orchestrator.  If that fails we default to
        `target` and let step 2 surface the underlying transport error so
        the message keeps its "top-level /stats unreachable" shape that
        callers (and tests) rely on."""
        try:
            orch = orchestrator_for(target, p["ravendb_domain"], db,
                                    p["client_cert"], p["ca_cert"])
        except Exception:
            orch = target
        path = "/databases/%s/stats/essential" % db
        try:
            status, body = request("GET", orch, p["ravendb_domain"], path,
                                   p["client_cert"], p["ca_cert"])
        except Exception as e:
            raise RuntimeError(
                "_aggregate_count: %s/%s top-level /stats unreachable: %r"
                % (target, db, e))
        if status != 200:
            raise RuntimeError(
                "_aggregate_count: %s/%s /stats/essential via %s HTTP %d"
                % (target, db, orch, status))
        return json.loads(body).get("CountOfDocuments")

    def predicate():
        s = _aggregate_count(src_target, src_db)
        t = _aggregate_count(tgt_target, tgt_db)
        if s == t:
            return True, "src=%d == tgt=%d" % (s, t)
        return False, "src=%d  tgt=%d  diff=%d" % (s, t, s - t)

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=3.0)
    if not done:
        raise RuntimeError(
            "TIMEOUT after %.1fs -- %s/%s doc count never equalled %s/%s; last: %s" %
            (elapsed, src_target, src_db, tgt_target, tgt_db, value))

    return ("DOC COUNT MATCHED -- %s/%s == %s/%s (took %.1fs, %s)" %
            (src_target, src_db, tgt_target, tgt_db, elapsed, value))


def k_hub_sink_doc_set_converged(p):
    hub_leader = p["hub_cluster_leader"]
    sink_leader = p["sink_cluster_leader"]
    db = p["db_name"]
    allowed = p["allowed_prefixes"] or []
    sink_local = p["sink_local_prefixes"] or []
    timeout = p["timeout"]
    interval = p["poll_interval"]
    sample_cap = int(p["sample_cap"] or 25)

    if not hub_leader or not sink_leader:
        raise ValueError(
            "kind=hub_sink_doc_set_converged requires `hub_cluster_leader` and `sink_cluster_leader`")
    if not allowed:
        raise ValueError(
            "kind=hub_sink_doc_set_converged requires `allowed_prefixes` (hub->sink filter)")

    def predicate():
        hub_ids = set(stream_all_doc_ids(
            hub_leader, p["ravendb_domain"], db, p["client_cert"], p["ca_cert"]))
        sink_ids = set(stream_all_doc_ids(
            sink_leader, p["ravendb_domain"], db, p["client_cert"], p["ca_cert"]))
        expected_on_sink = {i for i in hub_ids if prefix_match(i, allowed)}
        expected_on_hub  = {i for i in sink_ids if not prefix_match(i, sink_local)}
        missing_on_sink = sorted(expected_on_sink - sink_ids)
        missing_on_hub  = sorted(expected_on_hub - hub_ids)
        if not missing_on_sink and not missing_on_hub:
            return True, (len(hub_ids), len(sink_ids),
                          len(expected_on_sink), len(expected_on_hub))
        return False, (missing_on_sink, missing_on_hub)

    done, value, elapsed = poll_until(predicate, timeout=timeout, interval=interval)
    if not done:
        missing_on_sink, missing_on_hub = value
        lines = [
            "TIMEOUT after %.1fs -- hub-sink doc-set never converged" % elapsed,
            "  hub=%s  sink=%s  db=%s" % (hub_leader, sink_leader, db),
            "  filter (hub->sink): %s" % allowed,
            "  sink-local allowlist: %s" % (sink_local or "[]"),
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
        raise RuntimeError("\n".join(lines))

    hub_n, sink_n, exp_sink, exp_hub = value
    return ("CONVERGED -- hub-sink doc-set complete in %.1fs "
            "(hub=%d, sink=%d, expected-on-sink=%d, expected-on-hub=%d)"
            % (elapsed, hub_n, sink_n, exp_sink, exp_hub))


KINDS = {
    "leader":              k_leader,
    "member":              k_member,
    "rehab":               k_rehab,
    "etag_parity":         k_etag_parity,
    "docs_drain":          k_docs_drain,
    "quiescence":          k_quiescence,
    "stats_field_parity":  k_stats_field_parity,
    "conflicts_resolved":  k_conflicts_resolved,
    "marker_propagation":  k_marker_propagation,
    "doc_count_match":     k_doc_count_match,
    "hub_sink_doc_set_converged": k_hub_sink_doc_set_converged,
}


def main():
    module = AnsibleModule(argument_spec=dict(
        kind=dict(required=True, choices=list(KINDS)),
        ravendb_domain=dict(required=True),
        client_cert=dict(required=True, type="path"),
        ca_cert=dict(required=True, type="path"),
        # cluster / db scope
        db_name=dict(default=None),
        nodes=dict(type="list", elements="str", default=None),
        cluster_leader=dict(default=None),
        target=dict(default=None),
        target_tag=dict(default=None),
        expected_leader=dict(default=None),
        # marker_propagation
        source=dict(default=None),
        targets=dict(type="list", elements="str", default=None),
        marker_id_prefix=dict(default=None),
        # doc_count_match
        target_db_name=dict(default=None),
        # stats_field_parity
        fields=dict(type="list", elements="str", default=None),
        # hub_sink_doc_set_converged
        hub_cluster_leader=dict(default=None),
        sink_cluster_leader=dict(default=None),
        allowed_prefixes=dict(type="list", elements="str", default=None),
        sink_local_prefixes=dict(type="list", elements="str", default=None),
        sample_cap=dict(type="int", default=None),
        # timing
        timeout=dict(type="int", default=60),
        poll_interval=dict(type="int", default=2),
    ))

    handler = KINDS[module.params["kind"]]
    try:
        message = handler(module.params)
    except Exception as e:
        module.fail_json(msg=str(e))

    if isinstance(message, list):
        message = "\n".join(message)
    module.exit_json(changed=False, msg=message)


if __name__ == "__main__":
    main()
