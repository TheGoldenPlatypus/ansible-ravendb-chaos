"""
Tests for k_capture_cv.

What the kind does:  for each node, GET /stats and write the
                     DatabaseChangeVector to <output_dir>/<node>.cv.
                     Empty CV (DB has no docs) -> empty file (honest).
Returns:             one-line str summary.
Raises:              ValueError if output_dir missing.
                     RuntimeError if any node is unreachable.
"""

import os

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, write_doc


def test_writes_one_file_per_node(ravendb_cluster, tmp_path):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    out = str(tmp_path / "cv-dump")

    msg = diag.k_capture_cv(params(nodes=[node], output_dir=out))
    cv_file = os.path.join(out, f"{node}.cv")

    print(f"\n    expected: msg mentions '1 node'; file exists at <out>/{node}.cv")
    print(f"    actual:   msg={msg!r};  file exists = {os.path.exists(cv_file)}\n")
    assert "captured DatabaseChangeVector for 1 node" in msg
    assert os.path.exists(cv_file)


def test_after_writes_file_contains_a_real_cv(ravendb_cluster, tmp_path):
    """Once the DB has docs, the captured file contains a real CV string
    (matches the A:N-<dbid> shape), not an empty placeholder."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    out = str(tmp_path / "cv-dump")

    write_doc(node, "users/0", {"name": "Platypus"})

    diag.k_capture_cv(params(nodes=[node], output_dir=out))
    contents = open(os.path.join(out, f"{node}.cv")).read().strip()

    print(f"\n    expected: file contents look like a CV (e.g. 'A:1-<dbid>')")
    print(f"    actual:   {contents!r}\n")
    assert ":" in contents and "-" in contents and contents != ""


def test_missing_output_dir_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `output_dir`'")
    with pytest.raises(ValueError, match="requires `output_dir`") as exc:
        diag.k_capture_cv(params(nodes=[node], output_dir=None))
    print(f"    actual:   {exc.value!s}\n")


def test_multi_node_writes_distinct_cv_per_node(ravendb_cluster, tmp_path):
    """Two independent nodes (separate dbids) writing different docs ->
    two .cv files with two different contents."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]
    out = str(tmp_path / "cv-dump")

    write_doc(n1, "users/0", {"name": "on n1"})
    write_doc(n2, "users/0", {"name": "on n2"})

    diag.k_capture_cv(params(nodes=[n1, n2], output_dir=out))
    cv1 = open(os.path.join(out, f"{n1}.cv")).read().strip()
    cv2 = open(os.path.join(out, f"{n2}.cv")).read().strip()

    print(f"\n    expected: two files written, with different CV contents")
    print(f"    actual:   {n1}.cv = {cv1!r}")
    print(f"              {n2}.cv = {cv2!r}\n")
    assert cv1 != ""
    assert cv2 != ""
    assert cv1 != cv2


def test_unreachable_node_raises_loud(ravendb_cluster, tmp_path):
    """If ANY node in nodes is unreachable, raise -- don't silently write
    a '<UNAVAILABLE>' sentinel file that looks like a real capture."""
    info = ravendb_cluster(n_nodes=1)   # spin a real server so the fixture
                                         # cleans up TARGET_URL_OVERRIDES
    out = str(tmp_path / "cv-dump")

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_capture_cv(params(nodes=["dead-node"], output_dir=out))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)
