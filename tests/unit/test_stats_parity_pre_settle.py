"""
Tests for the stats_parity pre-settle loop.

What the kind does:  prints a per-node /stats table and asserts uniformity on
                     `assert_fields`.  With `settle_secs > 0`, it FIRST polls
                     until the asserted fields agree across all has_db nodes,
                     THEN runs the final assertion.

Why it exists:  RavenDB's per-node counters (CountOfDocuments, etc.) update
                asynchronously a few seconds behind the etag.  A preceding
                etag_parity / quiescence wait returns the instant the etag
                stabilizes but the counter on one node can still lag.  The
                pre-settle loop closes that race.

Pinned here:
  - settle_secs=0 (or unset) -> no settle loop, current behavior
  - settle_secs>0 with values agreeing first call -> no extra calls beyond
    the existing snapshot
  - settle_secs>0 with values disagreeing first call, agreeing second ->
    final assert PASSes
  - settle_secs>0 with values never agreeing -> raises TIMEOUT with the
    last-seen drift (not a silent pass)
"""

from collections import defaultdict
from unittest.mock import patch

import pytest

import ravendb_diagnostic as diag


def _params(**overrides):
    p = defaultdict(lambda: None)
    p["ravendb_domain"] = "ignored"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p["db_name"] = "db1"
    p["nodes"] = ["1a", "1b", "1c"]
    p["assert_fields"] = ["CountOfDocuments"]
    p["assert_mode"] = True
    p["settle_secs"] = 0
    p["settle_interval"] = 1
    for k, v in overrides.items():
        p[k] = v
    return p


def _wire_classify_and_stats(monkeypatch, per_node_snapshots, stats_per_node):
    """per_node_snapshots: list of {target:value, ...} maps -- one per poll
    iteration the kind makes for the asserted field.  stats_per_node:
    final {target: {field: value, ...}} dict returned by get_stats.

    classify_nodes is stubbed to return {1a:None, 1b:None, 1c:None}, skipped=[].
    per_node_field returns successive snapshots from per_node_snapshots.
    get_stats returns the final aggregated stats so the table renders."""
    monkeypatch.setattr(diag, "classify_nodes",
                        lambda p, nodes: ({"1a": None, "1b": None, "1c": None}, []))

    iterator = iter(per_node_snapshots)

    def fake_per_node_field(p, has_db, field):
        try:
            return next(iterator)
        except StopIteration:
            # If exhausted, return the last value (steady-state)
            return per_node_snapshots[-1]

    monkeypatch.setattr(diag, "per_node_field", fake_per_node_field)
    monkeypatch.setattr(diag, "get_stats",
                        lambda p, target, shard_id=None: stats_per_node.get(target))


def test_no_settle_when_settle_secs_zero(monkeypatch):
    """Default behavior unchanged -- no extra polling when settle_secs is 0.
    Header should explicitly say pre-settle is disabled so it's grep-able."""
    final_stats = {n: {"CountOfDocuments": 100} for n in ["1a", "1b", "1c"]}
    _wire_classify_and_stats(monkeypatch, [{"1a": 100, "1b": 100, "1c": 100}], final_stats)
    monkeypatch.setattr(diag.time, "sleep", lambda _s: None)

    lines = diag.k_stats_parity(_params(settle_secs=0))
    assert any("PASS" in line for line in lines)
    assert any("pre-settle=disabled" in line for line in lines)


def test_settle_passes_immediately_when_values_already_agree(monkeypatch):
    """settle_secs>0 with values agreeing on first snapshot -> no sleep at all.
    Header records pre-settle elapsed (~0s) and poll count (=1)."""
    sleep_calls = []
    monkeypatch.setattr(diag.time, "sleep", lambda s: sleep_calls.append(s))
    final_stats = {n: {"CountOfDocuments": 100} for n in ["1a", "1b", "1c"]}
    _wire_classify_and_stats(monkeypatch, [{"1a": 100, "1b": 100, "1c": 100}], final_stats)

    lines = diag.k_stats_parity(_params(settle_secs=60))

    assert any("PASS" in line for line in lines)
    assert sleep_calls == [], "should not sleep when values already agree"
    assert any("pre-settle=" in line and "(1 poll(s))" in line for line in lines)


def test_settle_polls_until_values_agree(monkeypatch):
    """First snapshot disagrees (1c short by 10), second agrees -> settle
    loop sleeps once, then assert passes."""
    sleep_calls = []
    monkeypatch.setattr(diag.time, "sleep", lambda s: sleep_calls.append(s))
    snapshots = [
        {"1a": 100, "1b": 100, "1c": 90},   # first poll: 1c lags
        {"1a": 100, "1b": 100, "1c": 100},  # second poll: caught up
    ]
    final_stats = {n: {"CountOfDocuments": 100} for n in ["1a", "1b", "1c"]}
    _wire_classify_and_stats(monkeypatch, snapshots, final_stats)

    lines = diag.k_stats_parity(_params(settle_secs=60, settle_interval=3))

    assert any("PASS" in line for line in lines)
    assert sleep_calls == [3], "should sleep exactly once between the two polls"
    assert any("(2 poll(s))" in line for line in lines), \
        "header should report poll count of 2 (one disagree + one agree)"


def test_settle_raises_timeout_when_never_converges(monkeypatch):
    """Counter on 1c never catches up -> kind raises with the last drift,
    no silent pass."""
    sleep_calls = []
    monkeypatch.setattr(diag.time, "sleep", lambda s: sleep_calls.append(s))

    # Fake monotonic clock that ticks 1s per call so we eventually exceed
    # the 2s budget; deadline test uses time.time() so we patch that too.
    tick = {"now": 0.0}
    def fake_time():
        tick["now"] += 1.5
        return tick["now"]
    monkeypatch.setattr(diag.time, "time", fake_time)

    drift_snapshot = {"1a": 100, "1b": 100, "1c": 90}
    final_stats = {n: {"CountOfDocuments": v} for n, v in drift_snapshot.items()}
    _wire_classify_and_stats(monkeypatch, [drift_snapshot, drift_snapshot, drift_snapshot],
                              final_stats)

    with pytest.raises(RuntimeError, match="pre-settle TIMEOUT") as ei:
        diag.k_stats_parity(_params(settle_secs=2, settle_interval=1))

    msg = str(ei.value)
    assert "CountOfDocuments" in msg
    assert "never converged" in msg
    # The timeout message now includes elapsed seconds + poll count for debugging.
    assert "poll(s)" in msg, "timeout should report number of polls before giving up"
