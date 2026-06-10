"""
Tests for get_backup_status's loud-failure semantics in ravendb_backup.

What the helper does:  GET /periodic-backup/status?name=<db>&taskId=<id> on a
                       node and return the parsed Status dict (or None when
                       the task has never produced a backup yet -- legitimate
                       200 + Status=null case).
Hardening focus:       previously every non-200 response collapsed to None,
                       which the polling loop in k_backup interpreted as
                       'still in progress' -- so a server returning HTTP 500
                       on every poll would silently use up the entire budget
                       and finally raise 'backup did not complete' when the
                       real cause was 'we never reached the endpoint'.

These are unit tests: ravendb_backup.request is monkeypatched to drive each
branch.
"""

import json
from collections import defaultdict

import pytest

import ravendb_backup as backup


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def test_returns_status_dict_on_200_with_data(monkeypatch):
    """Normal case: 200 with a populated Status field -> returns the dict."""
    expected_status = {
        "NodeTag": "A",
        "LastFullBackup": "2024-01-01T00:00:00Z",
        "LocalBackup": {"BackupDirectory": "/backups/db1"},
    }
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, json.dumps({"Status": expected_status}).encode()
    monkeypatch.setattr(backup, "request", fake)

    result = backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"\n    expected: returns the Status dict (NodeTag='A')")
    print(f"    actual:   {result!r}\n")
    assert result == expected_status


def test_returns_none_on_200_with_null_status(monkeypatch):
    """Legitimate 'task created but no backup has run yet': 200 with Status=null.
    Polling loop will treat this as 'keep waiting' -- which is correct here."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, json.dumps({"Status": None}).encode()
    monkeypatch.setattr(backup, "request", fake)

    result = backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"\n    expected: None (task has not produced a backup yet)")
    print(f"    actual:   {result!r}\n")
    assert result is None


def test_raises_on_http_500(monkeypatch):
    """The footgun this fix kills: previously HTTP 500 -> return None, polling
    loop kept waiting until budget ran out, then raised a misleading
    'backup did not complete'.  Now: raise loud immediately."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 500, b'{"error":"internal"}'
    monkeypatch.setattr(backup, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"    actual:   {exc.value!s}\n")


def test_raises_on_http_404(monkeypatch):
    """HTTP 404 (wrong db, wrong taskId, endpoint moved) was also collapsed
    to None before -- raise loud instead so the polling loop doesn't burn
    its budget polling a bad URL."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 404, b'{"error":"not found"}'
    monkeypatch.setattr(backup, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 404'")
    with pytest.raises(RuntimeError, match="HTTP 404") as exc:
        backup.get_backup_status(params(), "1a", "db1", 7)
    print(f"    actual:   {exc.value!s}\n")
