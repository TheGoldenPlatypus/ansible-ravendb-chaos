"""
Tests for k_schema_version.

What the kind does:  reports the RavenDB build version on each node.
                     Two optional asserts: require_parity (every node same
                     version) and expected_version (substring match).
Returns:             list[str]
Raises:              RuntimeError if NO node responded to /build/version.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def test_schema_version_reports_one_line_per_node(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    lines = diag.k_schema_version(params(target=node, nodes=[node]))

    print(f"\n    expected: lines[0] == 'schema/build version per node:'"
          f" and lines[1] contains '{node}'")
    print_lines("actual", lines)
    print()
    assert lines[0] == "schema/build version per node:"
    assert node in lines[1]


def test_expected_version_match_does_not_raise(ravendb_cluster):
    """assert_mode + expected_version contained in the real version => PASSes
    silently (returns lines, no raise)."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    # First read the real version, then assert against a substring of it.
    lines = diag.k_schema_version(params(nodes=[node]))
    actual_version_line = lines[1].strip()    # e.g. "1a  7.1.5"
    version_prefix = actual_version_line.split()[-1].split(".")[0]   # "7"

    print(f"\n    expected: no raise; expected_version='{version_prefix}' matches '{actual_version_line}'")
    result = diag.k_schema_version(
        params(nodes=[node], expected_version=version_prefix, assert_mode=True))
    print(f"    actual:   returned {result}\n")
    assert lines[0] == result[0]


def test_expected_version_mismatch_raises(ravendb_cluster):
    """assert_mode + a version substring the node doesn't have => raises
    DiagnosticViolation with the FAIL line."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises DiagnosticViolation mentioning 'nodes missing expected'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_schema_version(
            params(nodes=[node], expected_version="9.99.99", assert_mode=True))
    text = "\n".join(exc.value.lines)
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "nodes missing expected '9.99.99'" in text
    assert node in text


def test_all_nodes_unreachable_raises(ravendb_cluster):
    """When NO node responds to /build/version, fail loud -- silently returning
    an empty version map is a footgun."""
    info = ravendb_cluster(n_nodes=1)   # spin a real server so the fixture's
                                         # teardown restores TARGET_URL_OVERRIDES

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'no /build/version response from any node'")
    with pytest.raises(RuntimeError, match="no /build/version response from any node") as exc:
        diag.k_schema_version(params(nodes=["dead-node"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)
