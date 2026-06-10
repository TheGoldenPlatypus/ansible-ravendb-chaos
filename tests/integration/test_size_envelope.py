"""
Tests for k_size_envelope.

What the kind does:  reads per-node SizeOnDisk.  First call (baseline file
                     missing) writes the baseline; later calls compute %
                     growth per node and FAIL when any node exceeded
                     max_growth_pct (default 300%).
Returns:             list[str]
Raises:              RuntimeError if no node responded OR if a stored baseline
                     is 0 but current size is >0 (can't compute % growth).
"""

import json
import os

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def test_baseline_then_compare_no_growth(ravendb_cluster, tmp_path):
    """Capture baseline, immediately re-run: 0% growth, no FAIL."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    baseline_file = str(tmp_path / "size-baseline.json")

    first = diag.k_size_envelope(params(nodes=[node], baseline_file=baseline_file))
    print(f"\n    [first call] expected: 'BASELINE CAPTURED' line; file written")
    print(f"    [first call] actual:   {first}\n")
    assert any("BASELINE CAPTURED" in line for line in first)
    assert os.path.exists(baseline_file)

    second = diag.k_size_envelope(params(nodes=[node], baseline_file=baseline_file))
    text = "\n".join(second)
    print(f"    [second call] expected: 'size envelope check vs' and 'growth=+0.0%'")
    print(f"    [second call] actual:   {second}\n")
    assert "size envelope check vs" in text
    assert "growth=+0.0%" in text


def test_growth_above_threshold_fails_in_assert_mode(ravendb_cluster, tmp_path):
    """Stale baseline (1 byte) + current SizeOnDisk in megabytes => huge
    growth %. With assert_mode=True the kind raises DiagnosticViolation."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    baseline_file = str(tmp_path / "stale.json")

    with open(baseline_file, "w") as f:
        json.dump({node: 1}, f)   # 1-byte baseline

    print(f"\n    expected: raises DiagnosticViolation mentioning 'FAIL  growth > 300.0%'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_size_envelope(
            params(nodes=[node], baseline_file=baseline_file, assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  growth > 300.0%" in exc.value.lines[-1]
    assert node in exc.value.lines[-1]


def test_growth_above_threshold_info_only_without_assert_mode(ravendb_cluster, tmp_path):
    """Same stale baseline but assert_mode=False -> no raise, just appends an
    INFO line."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    baseline_file = str(tmp_path / "stale.json")

    with open(baseline_file, "w") as f:
        json.dump({node: 1}, f)

    lines = diag.k_size_envelope(params(nodes=[node], baseline_file=baseline_file))
    text = "\n".join(lines)
    print(f"\n    expected: no raise; text contains 'INFO  growth exceeds 300.0%'")
    print_lines("actual", lines)
    print()
    assert "INFO  growth exceeds 300.0%" in text


def test_baseline_zero_with_growth_fails_loud(ravendb_cluster, tmp_path):
    """If a stored baseline is 0 but the current size is >0, the % formula
    would divide by zero -- old behavior silently reported 0% growth (false
    negative).  New behavior: raise RuntimeError pointing at the stale baseline."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    baseline_file = str(tmp_path / "zero-baseline.json")

    with open(baseline_file, "w") as f:
        json.dump({node: 0}, f)   # zero baseline -- pre-data snapshot

    print(f"\n    expected: raises RuntimeError mentioning 'baseline=0 but current>0'")
    with pytest.raises(RuntimeError, match="baseline=0 but current>0") as exc:
        diag.k_size_envelope(params(nodes=[node], baseline_file=baseline_file))
    print(f"    actual:   {exc.value!s}\n")
    assert node in str(exc.value)


def test_all_nodes_unreachable_fails_loud(ravendb_cluster, tmp_path):
    """When NO node responds to /stats, the old behavior silently wrote an
    empty baseline file and exited successful.  New behavior: raise loud."""
    info = ravendb_cluster(n_nodes=1)   # spin a real server so the fixture
                                         # cleans up TARGET_URL_OVERRIDES

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    baseline_file = str(tmp_path / "would-be-empty.json")

    print(f"\n    expected: raises RuntimeError mentioning 'no /stats response from any node'")
    with pytest.raises(RuntimeError, match="no /stats response from any node") as exc:
        diag.k_size_envelope(params(nodes=["dead-node"], baseline_file=baseline_file))
    print(f"    actual:   {exc.value!s}\n")
    assert not os.path.exists(baseline_file)   # didn't write empty file
