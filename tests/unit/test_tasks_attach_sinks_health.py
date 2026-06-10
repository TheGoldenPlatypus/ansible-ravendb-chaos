"""
Tests for k_attach_sinks 'already present' health verification.

What the kind does:  k_attach_sinks (in ravendb_tasks) walks each sink leader
                     and creates a PullReplicationAsSink task.  If a task with
                     the expected name already exists on that sink, it
                     previously appended '(already present)' and continued --
                     blindly trusting the existing task.

Hardening focus:     A broken-but-existing sink-pull task (Disabled,
                     pointing at the wrong connection string, etc.) would
                     have silently survived a re-run of the scenario.  The
                     scenario would claim 'sinks attached' and the
                     replication chain would actually be dead.  New code
                     verifies the existing task's TaskState and
                     ConnectionStringName before claiming success.

Only the 'already present' branch is unit-testable here -- the create-new
path needs cert reading and several other heavy parts; integration tests
already cover that end-to-end.
"""

import json
from collections import defaultdict

import pytest

import ravendb_tasks as tasks


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def _wp(existing_task):
    """Returns a params bag + a fake `request` that simulates:
      - sink_task_lookup GET /tasks returns the given existing_task dict
      - PUT /admin/connection-strings returns 201 (success)"""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        if path.endswith("/tasks"):
            tasks_list = [existing_task] if existing_task else []
            return 200, json.dumps({"OngoingTasks": tasks_list}).encode()
        if "/admin/connection-strings" in path:
            return 201, b"{}"
        return 404, b"unexpected request"
    return fake


def test_attach_raises_when_existing_task_is_disabled(monkeypatch):
    """Task is found but TaskState=Disabled -> can't claim 'attached'."""
    existing = {
        "TaskType": "PullReplicationAsSink",
        "TaskName": "cluster2-to-hub",
        "TaskState": "Disabled",
        "ConnectionStringName": "hub-connection",
    }
    monkeypatch.setattr(tasks, "request", _wp(existing))

    print(f"\n    expected: RuntimeError mentioning 'unhealthy' and 'TaskState'")
    with pytest.raises(RuntimeError, match="unhealthy") as exc:
        tasks.k_attach_sinks(params(
            db_name="db1", hub_task_name="hub-task", sink_cluster_ids=[2],
            hub_topology_urls=["http://1a:8080"], replication_certs_dir="/tmp"))
    print(f"    actual:   {exc.value!s}\n")
    assert "TaskState" in str(exc.value)


def test_attach_raises_when_existing_task_has_wrong_connection_string(monkeypatch):
    """Task is enabled but ConnectionStringName doesn't match -- a previous
    run might have left a misconfigured task pointing at a different hub."""
    existing = {
        "TaskType": "PullReplicationAsSink",
        "TaskName": "cluster2-to-hub",
        "TaskState": "Enabled",
        "ConnectionStringName": "OLD-WRONG-CONN",
    }
    monkeypatch.setattr(tasks, "request", _wp(existing))

    print(f"\n    expected: RuntimeError mentioning 'ConnectionStringName' and 'OLD-WRONG-CONN'")
    with pytest.raises(RuntimeError, match="ConnectionStringName") as exc:
        tasks.k_attach_sinks(params(
            db_name="db1", hub_task_name="hub-task", sink_cluster_ids=[2],
            hub_topology_urls=["http://1a:8080"], replication_certs_dir="/tmp"))
    print(f"    actual:   {exc.value!s}\n")
    assert "OLD-WRONG-CONN" in str(exc.value)


def test_attach_succeeds_when_existing_task_is_healthy(monkeypatch):
    """Task is present, enabled, and connection-string matches the expected
    name -- safe to claim '(already present, verified healthy)'."""
    existing = {
        "TaskType": "PullReplicationAsSink",
        "TaskName": "cluster2-to-hub",
        "TaskState": "Enabled",
        "ConnectionStringName": "hub-connection",
    }
    monkeypatch.setattr(tasks, "request", _wp(existing))

    msg = tasks.k_attach_sinks(params(
        db_name="db1", hub_task_name="hub-task", sink_cluster_ids=[2],
        hub_topology_urls=["http://1a:8080"], replication_certs_dir="/tmp"))
    print(f"\n    expected: 'ATTACHED' message including 'verified healthy'")
    print(f"    actual:   {msg!r}\n")
    assert "verified healthy" in msg


def test_sink_task_lookup_returns_none_when_no_matching_task(monkeypatch):
    """Helper sanity: no task with the expected name in the OngoingTasks
    list -> returns None (caller will create one)."""
    other_task = {
        "TaskType": "PullReplicationAsSink",
        "TaskName": "different-name",
        "TaskState": "Enabled",
    }
    monkeypatch.setattr(tasks, "request", _wp(other_task))

    result = tasks.sink_task_lookup(params(), "2a", "db1", "2")
    print(f"\n    expected: None (no task with matching name)")
    print(f"    actual:   {result!r}\n")
    assert result is None


def test_sink_task_lookup_raises_on_http_500(monkeypatch):
    """GET /tasks returns 500 -- can't determine state, must raise."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        return 500, b'{"error":"fake"}'
    monkeypatch.setattr(tasks, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        tasks.sink_task_lookup(params(), "2a", "db1", "2")
    print(f"    actual:   {exc.value!s}\n")
