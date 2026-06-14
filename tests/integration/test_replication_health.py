"""
Tests for k_replication_health.

What the kind does:  fetches /databases/<db>/tasks, filters OngoingTasks to
                     replication-flavored types (ExternalReplication,
                     PullReplicationAsHub/Sink, RavenEtl), and under assert_mode
                     raises if any task is Faulted, has an Error, or is in
                     TaskConnectionStatus=Reconnect.
Returns:             list[str] -- header + per-task line + PASS line.
                     Raises RuntimeError on non-200 (db missing / node unreachable).
                     Raises DiagnosticViolation under assert_mode on any stuck task.
"""

import time

import pytest

import ravendb_diagnostic as diag
from ravendb_diagnostic import DiagnosticViolation
from raven_lab import (
    _http,
    _url_for,
    params,
    print_lines,
    setup_external_replication,
    write_doc,
)


def test_passes_when_replication_task_is_healthy(ravendb_cluster):
    """External replication wired between two reachable nodes -> task reports
    Active connection status -> health check returns PASS."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    setup_external_replication(src_node=n1, dst_node=n2)
    write_doc(n1, "users/1", {"name": "trigger"})

    lines = _wait_for_pass_or_fail(n1)
    text = "\n".join(lines)

    print(f"\n    expected: PASS + one Replication-type task on {n1}/db1")
    print_lines("actual", lines)
    print()
    assert f"replication health on {n1}/db1" in lines[0]
    # RavenDB reports an external-replication task under TaskType="Replication"
    # in the OngoingTasks list (the create endpoint uses external-replication
    # in its URL, but the listed type is plain "Replication").
    assert "Replication" in text
    assert "PASS  no stuck replication task" in text


def test_passes_when_no_replication_tasks_exist(ravendb_cluster):
    """Fresh db with no tasks configured -> the kind reports 0 tasks and
    PASSes.  No false positive on an empty task list."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_replication_health(params(target=node, assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: header reports 0 task(s) on {node}/db1, PASS")
    print_lines("actual", lines)
    print()
    assert f"replication health on {node}/db1  (0 replication task(s))" in lines[0]
    assert "PASS  no stuck replication task" in text


def test_fails_loud_under_assert_mode_when_task_is_stuck(ravendb_cluster):
    """Wire an ExternalReplication task to a port nothing listens on -- the
    task lands in TaskConnectionStatus=Reconnect within a few seconds.  Under
    assert_mode, the kind must raise DiagnosticViolation."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    _wire_bogus_external_replication(src_node=node)

    # Trigger the OUTGOING attempt so the task actually tries to connect.
    write_doc(node, "users/1", {"name": "trigger"})

    # Wait for the task to enter a stuck state (Reconnect / error).
    last_lines = []
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            last_lines = diag.k_replication_health(params(target=node, assert_mode=True))
        except DiagnosticViolation as e:
            print(f"\n    expected: DiagnosticViolation mentioning stuck task")
            print_lines("actual", e.lines)
            print()
            text = "\n".join(e.lines)
            assert "FAIL  stuck task" in text
            return
        time.sleep(0.5)

    print_lines("last seen (no violation raised within 20s)", last_lines)
    pytest.fail("expected DiagnosticViolation; task never went stuck")


def test_raises_when_db_does_not_exist(ravendb_cluster):
    """Fail loud on missing DB instead of returning a soft string."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_replication_health(params(target=node, db_name="does-not-exist"))
    print(f"    actual:   {exc.value!s}\n")
    assert f"{node}/does-not-exist" in str(exc.value)


def _wait_for_pass_or_fail(node, timeout=10):
    """Poll k_replication_health(node) until the task list is non-empty.  The
    task is registered async by RavenDB so a fresh setup can briefly report 0
    tasks before settling."""
    deadline = time.monotonic() + timeout
    lines = []
    while time.monotonic() < deadline:
        lines = diag.k_replication_health(params(target=node))
        if "(0 replication task(s))" not in lines[0]:
            return lines
        time.sleep(0.5)
    return lines


def _wire_bogus_external_replication(src_node, db="db1"):
    """Create a connection string + external-replication task pointing at a
    port that nothing listens on, so the task lands in Reconnect."""
    src_url = _url_for(src_node)
    bogus_url = "http://127.0.0.1:1"   # port 1 -> connection refused

    cs_body = {
        "Type": "Raven",
        "Name": "bogus-conn",
        "Database": db,
        "TopologyDiscoveryUrls": [bogus_url],
    }
    status, resp = _http(
        "PUT", f"{src_url}/databases/{db}/admin/connection-strings", body=cs_body)
    if status not in (200, 201):
        raise RuntimeError(f"connection-string PUT failed: HTTP {status} {resp[:300]}")

    task_body = {
        "Watcher": {
            "Name": "bogus-ext-repl",
            "ConnectionStringName": "bogus-conn",
            "Database": db,
            "Url": bogus_url,
            "Disabled": False,
        }
    }
    status, resp = _http(
        "POST", f"{src_url}/databases/{db}/admin/tasks/external-replication",
        body=task_body)
    if status not in (200, 201):
        raise RuntimeError(f"bogus external-replication POST failed: HTTP {status} {resp[:300]}")
