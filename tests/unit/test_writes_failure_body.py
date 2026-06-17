"""
Tests for the failure-body capture in ravendb_writes._format_failure.

What changed:  every write kind (k_docs, k_docs_freeform, k_docs_revisions,
               k_docs_interleaved, k_attachments, k_counters, k_timeseries,
               k_delete) used to surface failures as just "doc_id -> HTTP 500"
               -- the server's error message was discarded.  The new helper
               pulls Message/Error out of RavenDB's JSON error envelope
               (or a 200-char raw snippet when it's not JSON) so '41/2000
               PUTs failed' messages carry the actual exception text and
               can be debugged without a packet capture.

Pinned here:
  - JSON error envelope with Message -> snippet appears in failure entry
  - JSON envelope with only Error (no Message) -> Error appears in entry
  - Plain text body (non-JSON 500 page) -> truncated snippet appears
  - Empty body -> message is just "label -> HTTP <status>" (no trailing colon)
  - Snippet is truncated to 200 chars so the failure list stays readable
  - k_docs end-to-end: server returns 500 with a real-shaped error -> the
    RuntimeError that the kind raises includes the server's Message
"""

from collections import defaultdict
import json

import pytest

import ravendb_writes as writes


def test_format_failure_extracts_message_from_json_envelope():
    body = json.dumps({
        "Type": "Raven.Server.Documents.MergedDocumentTransactionException",
        "Message": "Transaction merger queue is full; try again later",
    }).encode()
    out = writes._format_failure("users/0", 500, body)
    assert "users/0" in out
    assert "HTTP 500" in out
    assert "Transaction merger queue is full" in out


def test_format_failure_falls_back_to_error_field():
    body = json.dumps({
        "Type": "X",
        "Error": "Internal stack: at ... at ...",
    }).encode()
    out = writes._format_failure("users/0", 500, body)
    assert "Internal stack" in out


def test_format_failure_uses_raw_body_when_not_json():
    body = b"<html><body>503 Service Unavailable</body></html>"
    out = writes._format_failure("users/0", 503, body)
    assert "HTTP 503" in out
    assert "503 Service Unavailable" in out


def test_format_failure_omits_colon_when_body_empty():
    out = writes._format_failure("users/0", 500, b"")
    assert out == "users/0 -> HTTP 500"
    assert out.endswith("500")   # no trailing ": "


def test_format_failure_truncates_long_messages_to_200_chars():
    long = "x" * 500
    body = json.dumps({"Message": long}).encode()
    out = writes._format_failure("users/0", 500, body)
    # snippet contributes <= 200 chars; whole entry can be a bit longer due to prefix
    assert "x" * 200 in out
    assert "x" * 201 not in out


def test_k_docs_failure_message_includes_server_exception(monkeypatch):
    """End-to-end via the kind: when the server returns 500 with a real
    RavenDB error envelope, the raised RuntimeError must contain the
    server's Message text -- not just 'HTTP 500'."""
    server_body = json.dumps({
        "Type": "Raven.Server.Documents.SomethingException",
        "Message": "License does not allow more than 10 docs",
    }).encode()

    def fake_put_idempotent(p, target, path, body):
        return 500, server_body
    monkeypatch.setattr(writes, "put_idempotent", fake_put_idempotent)
    monkeypatch.setattr(writes.time, "sleep", lambda _s: None)

    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["target"] = "1a"
    p["count"] = 2
    p["id_prefix"] = "users"

    with pytest.raises(RuntimeError, match="License does not allow") as exc:
        writes.k_docs(p)
    assert "HTTP 500" in str(exc.value)
