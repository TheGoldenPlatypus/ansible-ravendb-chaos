"""
Tests for the hub_sink_doc_set diagnostic kind and its wait wrapper
hub_sink_doc_set_converged.

Both kinds rely on exhaustive enumeration of doc ids from hub + sink via
stream_all_doc_ids, then apply set containment with the scenario's filter
and sink-local allowlist.  We mock stream_all_doc_ids directly so the tests
have no network dependency.

Pinned here:
  - PASS path:  every hub doc that matches the filter is on sink; every
    sink doc that is not sink-local is on hub.
  - FAIL: a hub doc matching the filter is missing on sink.
  - FAIL: a sink doc that's not sink-local is missing on hub (leak / orphan).
  - PASS: a sink doc with a sink-local prefix is exempt from the hub check.
  - FAIL: a hub doc that does NOT match the filter and is missing on sink is
    correctly IGNORED (filter says it shouldn't be there).
  - prefix_match honors both 'users/sink1/*' and 'sink1-local/' shapes.
  - wait kind returns CONVERGED when predicate flips PASS within the budget.
  - wait kind raises TIMEOUT with the actual missing-id lists when it doesn't.
"""

from collections import defaultdict
from unittest.mock import patch

import pytest

import ravendb_diagnostic as diag
import ravendb_wait as wait_mod
from ansible.module_utils.ravendb_client import prefix_match


def params(**overrides):
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["hub_cluster_leader"] = "1a"
    p["sink_cluster_leader"] = "2a"
    p["allowed_prefixes"] = ["users/sink1/*"]
    p["sink_local_prefixes"] = ["sink1-local/"]
    p["sample_cap"] = 25
    p["assert_mode"] = True
    for k, v in overrides.items():
        p[k] = v
    return p


# ---------------------------------------------------------------------------- prefix_match

def test_prefix_match_handles_star_suffix():
    assert prefix_match("users/sink1/0", ["users/sink1/*"])
    assert prefix_match("users/sink1/family/doc/0", ["users/sink1/*"])
    assert not prefix_match("users/sink2/0", ["users/sink1/*"])


def test_prefix_match_handles_slash_suffix():
    assert prefix_match("sink1-local/users/0", ["sink1-local/"])
    assert not prefix_match("users/sink1/0", ["sink1-local/"])


def test_prefix_match_empty_list_is_false():
    assert not prefix_match("anything", [])
    assert not prefix_match("anything", None)


# ---------------------------------------------------------------------------- diagnostic

def _wire_streams(monkeypatch, hub_ids, sink_ids):
    def fake_stream(target, *a, **kw):
        if target == "1a":
            return list(hub_ids)
        if target == "2a":
            return list(sink_ids)
        raise AssertionError("unexpected target %r" % target)
    monkeypatch.setattr(diag, "stream_all_doc_ids", fake_stream)


def test_diag_pass_when_everything_is_where_it_should_be(monkeypatch):
    hub = ["users/sink1/0", "users/sink1/1", "orders/hub/0"]
    sink = ["users/sink1/0", "users/sink1/1", "sink1-local/users/0"]
    _wire_streams(monkeypatch, hub, sink)

    out = diag.k_hub_sink_doc_set(params())

    print("\n    expected: PASS line present\n    actual:   %s\n" % out[-1])
    assert any("PASS" in line for line in out)


def test_diag_fail_missing_on_sink(monkeypatch):
    # users/sink1/1 is on hub, matches filter, but absent from sink
    hub = ["users/sink1/0", "users/sink1/1"]
    sink = ["users/sink1/0"]
    _wire_streams(monkeypatch, hub, sink)

    with pytest.raises(diag.DiagnosticViolation) as ei:
        diag.k_hub_sink_doc_set(params())

    msg = "\n".join(ei.value.lines)
    print("\n    expected: FAIL mentioning users/sink1/1 missing on sink\n    actual: %s\n" % msg)
    assert "MISSING on sink: 1" in msg
    assert "users/sink1/1" in msg


def test_diag_fail_missing_on_hub_when_not_sink_local(monkeypatch):
    # sink-only doc that is NOT under the sink-local allowlist = leak (should be on hub)
    hub = ["users/sink1/0"]
    sink = ["users/sink1/0", "users/sink1/extra/0"]
    _wire_streams(monkeypatch, hub, sink)

    with pytest.raises(diag.DiagnosticViolation) as ei:
        diag.k_hub_sink_doc_set(params())

    msg = "\n".join(ei.value.lines)
    print("\n    expected: FAIL mentioning users/sink1/extra/0 missing on hub\n    actual: %s\n" % msg)
    assert "MISSING on hub:  1" in msg
    assert "users/sink1/extra/0" in msg


def test_diag_sink_local_doc_is_exempt_from_hub_check(monkeypatch):
    # sink1-local/* exists only on sink -- that's expected, scenario design.
    hub = ["users/sink1/0"]
    sink = ["users/sink1/0", "sink1-local/users/0", "sink1-local/users/1"]
    _wire_streams(monkeypatch, hub, sink)

    out = diag.k_hub_sink_doc_set(params())

    print("\n    expected: PASS (sink-local docs are exempt)\n    actual:   %s\n" % out[-1])
    assert any("PASS" in line for line in out)


def test_diag_hub_doc_not_matching_filter_is_ignored(monkeypatch):
    # orders/hub/* doesn't match users/sink1/* -- filter says it shouldn't
    # cross to sink, so its absence from sink is NOT a failure.
    hub = ["users/sink1/0", "orders/hub/0", "orders/hub/1"]
    sink = ["users/sink1/0"]
    _wire_streams(monkeypatch, hub, sink)

    out = diag.k_hub_sink_doc_set(params())

    print("\n    expected: PASS (orders/hub/* is filtered out of replication)\n    actual: %s\n" % out[-1])
    assert any("PASS" in line for line in out)


def test_diag_reports_both_sides_at_once(monkeypatch):
    hub = ["users/sink1/0", "users/sink1/1"]              # /1 missing on sink
    sink = ["users/sink1/0", "stray/orphan/0"]            # stray missing on hub
    _wire_streams(monkeypatch, hub, sink)

    with pytest.raises(diag.DiagnosticViolation) as ei:
        diag.k_hub_sink_doc_set(params())

    msg = "\n".join(ei.value.lines)
    print("\n    expected: both sides reported\n    actual:\n%s\n" % msg)
    assert "MISSING on sink: 1" in msg
    assert "MISSING on hub:  1" in msg


def test_diag_rejects_missing_allowed_prefixes():
    p = params(allowed_prefixes=None)
    with pytest.raises(ValueError, match="allowed_prefixes"):
        diag.k_hub_sink_doc_set(p)


# ---------------------------------------------------------------------------- wait

def test_wait_converges_when_sink_catches_up(monkeypatch):
    # Per-target call counters.  First sink call returns the incomplete set; from
    # the second sink call onward, sink has caught up.  Hub is always complete.
    hub_calls = {"n": 0}
    sink_calls = {"n": 0}

    def fake_stream(target, *a, **kw):
        if target == "1a":
            hub_calls["n"] += 1
            return ["users/sink1/0", "users/sink1/1"]
        sink_calls["n"] += 1
        if sink_calls["n"] == 1:
            return ["users/sink1/0"]
        return ["users/sink1/0", "users/sink1/1"]

    monkeypatch.setattr(wait_mod, "stream_all_doc_ids", fake_stream)
    monkeypatch.setattr(wait_mod.time, "sleep", lambda _s: None)

    p = params(timeout=10, poll_interval=1)
    msg = wait_mod.k_hub_sink_doc_set_converged(p)

    print("\n    expected: CONVERGED after 2 sink polls\n    actual:   %s (sink_calls=%d)\n"
          % (msg, sink_calls["n"]))
    assert msg.startswith("CONVERGED")
    assert sink_calls["n"] >= 2


def test_wait_timeout_reports_missing_ids(monkeypatch):
    def fake_stream(target, *a, **kw):
        if target == "1a":
            return ["users/sink1/0", "users/sink1/1"]
        return ["users/sink1/0"]
    monkeypatch.setattr(wait_mod, "stream_all_doc_ids", fake_stream)
    monkeypatch.setattr(wait_mod.time, "sleep", lambda _s: None)

    p = params(timeout=1, poll_interval=1)
    with pytest.raises(RuntimeError) as ei:
        wait_mod.k_hub_sink_doc_set_converged(p)

    msg = str(ei.value)
    print("\n    expected: TIMEOUT mentioning users/sink1/1\n    actual:\n%s\n" % msg)
    assert "TIMEOUT" in msg
    assert "users/sink1/1" in msg
    assert "MISSING on sink: 1" in msg
