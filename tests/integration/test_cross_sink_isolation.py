"""
Tests for k_cross_sink_isolation.

What the kind does:  on one sink leader, probes `<prefix>/0..N-1` for each
                     forbidden prefix.  Any 200 means a forbidden doc leaked
                     to this sink => FAIL.
Returns:             list[str]
Raises:              ValueError if `forbidden_prefixes` is missing.
                     DiagnosticViolation in assert_mode on any leak.
                     RuntimeError if the sink leader is unreachable.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_passes_when_sink_has_only_allowed_docs(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    # Allowed docs on the sink -- nothing under the forbidden prefix.
    write_doc(sink, "users/sink1/0", {"name": "ok"})

    lines = diag.k_cross_sink_isolation(params(
        sink_cluster_leader=sink,
        forbidden_prefixes=["users/sink2"],
        sample_per_prefix=10,
        assert_mode=True,
    ))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'no forbidden-prefix docs on this sink'")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "leaks: 0" in text


def test_fails_when_a_forbidden_doc_is_present(ravendb_cluster):
    """One forbidden doc on the sink -> FAIL line + the leak shows up."""
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    write_doc(sink, "users/sink2/0", {"name": "leak"})    # forbidden

    lines = diag.k_cross_sink_isolation(params(
        sink_cluster_leader=sink,
        forbidden_prefixes=["users/sink2"],
        sample_per_prefix=10,
    ))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  cross-sink leak' and the leaked id 'users/sink2/0'")
    print_lines("actual", lines)
    print()
    assert "FAIL  cross-sink leak" in text
    assert "users/sink2/0" in text


def test_assert_mode_raises_on_leak(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    write_doc(sink, "users/sink2/0", {"name": "leak"})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  cross-sink leak'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_cross_sink_isolation(params(
            sink_cluster_leader=sink,
            forbidden_prefixes=["users/sink2"],
            sample_per_prefix=10,
            assert_mode=True,
        ))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  cross-sink leak" in exc.value.lines[-1]


def test_missing_forbidden_prefixes_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    sink = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `forbidden_prefixes`'")
    with pytest.raises(ValueError, match="requires `forbidden_prefixes`") as exc:
        diag.k_cross_sink_isolation(params(
            sink_cluster_leader=sink,
            forbidden_prefixes=None,
        ))
    print(f"    actual:   {exc.value!s}\n")


def test_unreachable_sink_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-sink"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_cross_sink_isolation(params(
            sink_cluster_leader="dead-sink",
            forbidden_prefixes=["users/sink2"],
            sample_per_prefix=5,
        ))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-sink" in str(exc.value)
