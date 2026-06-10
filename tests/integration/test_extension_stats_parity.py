"""
Tests for k_extension_stats_parity.

What the kind does:  like stats_parity but only for the extension counts --
                     attachments / counters / timeseries.  Configurable via
                     `aspects` CSV (default = all three).  PASSes if every
                     selected field matches across nodes; FAILs on drift.
Returns:             list[str]
Raises:              ValueError if no probed node has the database.
                     DiagnosticViolation in assert_mode on drift.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import add_attachment, params, print_lines, write_doc


def test_passes_when_two_empty_dbs_have_no_extensions(ravendb_cluster):
    """Both nodes start with empty DBs -> all extension counts are 0 on both
    -> PASS regardless of which aspects are checked."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    lines = diag.k_extension_stats_parity(params(nodes=[n1, n2]))
    text = "\n".join(lines)

    print(f"\n    expected: PASS (no drift, all zeros)")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "FAIL" not in text


def test_fails_when_attachment_count_drifts(ravendb_cluster):
    """One node has an attachment, the other doesn't -> CountOfAttachments
    differs -> FAIL line lists 'CountOfAttachments' as drifted."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "Alice"})
    add_attachment(n1, "users/0", "data", "hello bytes")

    lines = diag.k_extension_stats_parity(params(nodes=[n1, n2]))
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  drift on' line containing 'CountOfAttachments'")
    print_lines("actual", lines)
    print()
    assert "FAIL  drift on" in text
    assert "CountOfAttachments" in lines[-1]


def test_assert_mode_raises_on_drift(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "Alice"})
    add_attachment(n1, "users/0", "data", "hello")

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  drift on'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_extension_stats_parity(
            params(nodes=[n1, n2], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  drift on" in exc.value.lines[-1]


def test_aspect_filter_narrows_what_is_checked(ravendb_cluster):
    """Same attachment drift, but aspects='counters' -> kind only inspects
    CountOfCounterEntries (both 0) -> PASS, attachment drift ignored."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "Alice"})
    add_attachment(n1, "users/0", "data", "hello")

    lines = diag.k_extension_stats_parity(
        params(nodes=[n1, n2], aspects="counters"))
    text = "\n".join(lines)

    print(f"\n    expected: PASS (only counters checked, both zero)")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "FAIL" not in text


def test_all_nodes_unreachable_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises ValueError mentioning 'no probed node has database'")
    with pytest.raises(ValueError, match="no probed node has database") as exc:
        diag.k_extension_stats_parity(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
