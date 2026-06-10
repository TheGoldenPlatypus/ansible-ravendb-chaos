"""
Tests for the k_setup_etl JSON / TaskId / toggle hardening.

What the kind does:  creates a Raven ETL connection string + task on the source
                     cluster, then runs a disable/enable cycle on the task to
                     force it to (re)start cleanly.
Hardening focus:     (1) the TaskId from the PUT /admin/etl response MUST be
                     present -- a missing TaskId silently skipped the toggle
                     step but still claimed 'ETL configured';
                     (2) JSON parse errors must NOT be swallowed -- garbage
                     in the response body is a real bug worth raising on;
                     (3) the disable/enable toggle responses MUST be checked
                     -- previous version fire-and-forgot them.

These are unit tests: ravendb_tasks.request is monkeypatched to drive each
branch deterministically.
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


def _wp():
    """Common k_setup_etl params bag."""
    return params(
        target="1a", db_name="db1", task_name="rp1-etl",
        target_db_name="db1-mirror", target_topology_urls=["http://2a:8080"],
        script=None, collections=None,
    )


def _route(handlers):
    """Build a fake request() that dispatches by (METHOD, path-substring) to
    handlers.  Each handler returns (status, body_bytes).  An unhandled call
    raises so the test fails loudly instead of silently returning None."""
    def fake(method, target, domain, path, client_cert, ca_cert,
            body=None, content_type=None, timeout=30):
        for (m, sub), h in handlers.items():
            if m == method and sub in path:
                return h(method, target, path, body)
        raise AssertionError(f"unhandled request: {method} {path}")
    return fake


def _admin_db_ok(*_a, **_k):
    """assert_target_hosts_db requires the source_leader's tag in Members
    (or a sharded layout).  Source leader in _wp() is '1a' -> tag 'A'."""
    return 200, json.dumps({
        "Sharding": {},
        "Topology": {"Members": ["A"], "Promotables": [], "Rehabs": []},
    }).encode()


def test_missing_target_db_raises():
    print(f"\n    expected: ValueError mentioning 'requires `target_db_name`'")
    with pytest.raises(ValueError, match="requires `target_db_name`") as exc:
        tasks.k_setup_etl(params(target="1a", task_name="x",
                                  target_topology_urls=["http://2a"]))
    print(f"    actual:   {exc.value!s}\n")


def test_etl_response_missing_TaskId_raises(monkeypatch):
    """PUT /admin/etl returns 201 but the body has no TaskId -- previously this
    silently skipped the toggle and still reported 'ETL configured'.  Must
    raise loud instead."""
    handlers = {
        ("GET", "/admin/databases"): _admin_db_ok,
        ("PUT", "/admin/connection-strings"): lambda *_: (201, b"{}"),
        ("PUT", "/admin/etl"): lambda *_: (201, b'{"SomeOtherField":42}'),  # no TaskId
    }
    monkeypatch.setattr(tasks, "request", _route(handlers))

    print(f"\n    expected: RuntimeError mentioning 'returned no TaskId'")
    with pytest.raises(RuntimeError, match="returned no TaskId") as exc:
        tasks.k_setup_etl(_wp())
    print(f"    actual:   {exc.value!s}\n")


def test_etl_response_garbage_json_raises(monkeypatch):
    """Body isn't valid JSON -- previously swallowed by `except: pass`.  Must
    raise loud now."""
    handlers = {
        ("GET", "/admin/databases"): _admin_db_ok,
        ("PUT", "/admin/connection-strings"): lambda *_: (201, b"{}"),
        ("PUT", "/admin/etl"): lambda *_: (201, b"not-json"),
    }
    monkeypatch.setattr(tasks, "request", _route(handlers))

    print(f"\n    expected: raises json.JSONDecodeError (no longer swallowed)")
    with pytest.raises(json.JSONDecodeError) as exc:
        tasks.k_setup_etl(_wp())
    print(f"    actual:   {exc.value!s}\n")


def test_toggle_failure_raises(monkeypatch):
    """Task created cleanly; the disable=true call returns HTTP 500.  Previous
    version ignored the status and reported success."""
    state = {"toggle_calls": 0}
    def toggle(method, target, path, body):
        state["toggle_calls"] += 1
        if state["toggle_calls"] == 1:
            return 500, b'{"error":"fake-toggle-failure"}'
        return 200, b""
    handlers = {
        ("GET", "/admin/databases"): _admin_db_ok,
        ("PUT", "/admin/connection-strings"): lambda *_: (201, b"{}"),
        ("PUT", "/admin/etl"): lambda *_: (201, json.dumps({"TaskId": 7}).encode()),
        ("POST", "/admin/tasks/state"): toggle,
    }
    monkeypatch.setattr(tasks, "request", _route(handlers))

    print(f"\n    expected: RuntimeError mentioning 'toggle disable=true' and 'HTTP 500'")
    with pytest.raises(RuntimeError, match="toggle disable=true.*HTTP 500") as exc:
        tasks.k_setup_etl(_wp())
    print(f"    actual:   {exc.value!s}\n")


def test_happy_path_reports_etl_configured(monkeypatch):
    """End-to-end happy path: every call returns ok, TaskId=42, both toggles
    return 200.  Final message should report 'ETL configured'."""
    handlers = {
        ("GET", "/admin/databases"): _admin_db_ok,
        ("PUT", "/admin/connection-strings"): lambda *_: (201, b"{}"),
        ("PUT", "/admin/etl"): lambda *_: (201, json.dumps({"TaskId": 42}).encode()),
        ("POST", "/admin/tasks/state"): lambda *_: (200, b""),
    }
    monkeypatch.setattr(tasks, "request", _route(handlers))

    msg = tasks.k_setup_etl(_wp())
    print(f"\n    expected: 'ETL configured -- task rp1-etl on 1a/db1 ...'")
    print(f"    actual:   {msg!r}\n")
    assert "ETL configured -- task 'rp1-etl' on 1a/db1" in msg
