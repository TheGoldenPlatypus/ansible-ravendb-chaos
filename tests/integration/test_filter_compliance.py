"""
Tests for k_filter_compliance.

What the kind does:  on one sink leader, lists every doc and FAILs if any id
                     doesn't start with one of `allowed_prefixes`.  If
                     `allowed_prefixes` is omitted, the kind auto-discovers
                     from DatabaseRecord.SinkPullReplications (not exercised
                     here on the populated path -- too heavy without PFX setup).
Returns:             list[str]
Raises:              RuntimeError if no allowed_prefixes can be resolved,
                     or if the sink is unreachable.
                     DiagnosticViolation in assert_mode on any leak.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_passes_when_every_doc_matches_an_allowed_prefix(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    write_doc(sink, "users/sink1/0", {"name": "ok"})
    write_doc(sink, "users/sink1/1", {"name": "ok"})

    lines = diag.k_filter_compliance(params(
        sink_cluster_leader=sink,
        allowed_prefixes=["users/sink1/"],
        assert_mode=True,
    ))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'every sink doc matches an allowed prefix'; leak ids: 0")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "leak ids (no allowed prefix matched): 0" in text


def test_fails_when_a_doc_does_not_match_any_allowed_prefix(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    write_doc(sink, "users/sink1/0", {"name": "ok"})
    write_doc(sink, "orders/leak/0", {"name": "wrong-prefix"})   # leak

    lines = diag.k_filter_compliance(params(
        sink_cluster_leader=sink,
        allowed_prefixes=["users/sink1/"],
    ))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  filter leak' line + the offending id 'orders/leak/0' in sample")
    print_lines("actual", lines)
    print()
    assert "FAIL  filter leak" in text
    assert "orders/leak/0" in text


def test_assert_mode_raises_on_leak(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    write_doc(sink, "orders/leak/0", {"name": "wrong-prefix"})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  filter leak'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_filter_compliance(params(
            sink_cluster_leader=sink,
            allowed_prefixes=["users/sink1/"],
            assert_mode=True,
        ))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  filter leak" in exc.value.lines[-1]


def test_auto_discovery_with_no_sink_pull_tasks_raises(ravendb_cluster):
    """Auto-discovery code path: caller omits allowed_prefixes, kind reads
    DatabaseRecord.SinkPullReplications, finds none -> RuntimeError.  Exercises
    the empty-fallback branch."""
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'could not resolve allowed_prefixes'")
    with pytest.raises(RuntimeError, match="could not resolve allowed_prefixes") as exc:
        diag.k_filter_compliance(params(
            sink_cluster_leader=sink,
            allowed_prefixes=None,
        ))
    print(f"    actual:   {exc.value!s}\n")


def test_unreachable_sink_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-sink"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_filter_compliance(params(
            sink_cluster_leader="dead-sink",
            allowed_prefixes=["users/sink1/"],
        ))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-sink" in str(exc.value)
