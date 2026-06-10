"""
Tests for k_capture_doc_cv.

What the kind does:  for each (node, doc_id) pair, GET /databases/<db>/docs?id=<id>
                     and write the doc's @change-vector to
                     <output_dir>/<node>__<safe_id>.cv.  Missing docs are recorded
                     as '<NOT_FOUND status=...>' so the file set stays uniform.
Returns:             one-line str summary.
Raises:              ValueError if `ids` or `output_dir` is missing.
                     RuntimeError if any node is unreachable.
"""

import os

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, write_doc


def test_missing_ids_raises(ravendb_cluster, tmp_path):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `ids`'")
    with pytest.raises(ValueError, match="requires `ids`") as exc:
        diag.k_capture_doc_cv(params(nodes=[node], output_dir=str(tmp_path)))
    print(f"    actual:   {exc.value!s}\n")


def test_missing_output_dir_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `output_dir`'")
    with pytest.raises(ValueError, match="requires `output_dir`") as exc:
        diag.k_capture_doc_cv(params(nodes=[node], ids=["users/0"], output_dir=None))
    print(f"    actual:   {exc.value!s}\n")


def test_happy_path_one_node_two_docs(ravendb_cluster, tmp_path):
    """Two docs written, two files captured, each containing a real CV."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    out = str(tmp_path / "doc-cv")

    write_doc(node, "users/0", {"name": "Alice"})
    write_doc(node, "users/1", {"name": "Bob"})

    msg = diag.k_capture_doc_cv(
        params(nodes=[node], ids=["users/0", "users/1"], output_dir=out))
    file0 = open(os.path.join(out, f"{node}__users_0.cv")).read().strip()
    file1 = open(os.path.join(out, f"{node}__users_1.cv")).read().strip()

    print(f"\n    expected: msg mentions '2 per-(node,id)'; both files contain real CVs")
    print(f"    actual:   msg={msg!r}")
    print(f"              {node}__users_0.cv = {file0!r}")
    print(f"              {node}__users_1.cv = {file1!r}\n")
    assert "captured 2 per-(node,id)" in msg
    assert ":" in file0 and "-" in file0 and "<NOT_FOUND" not in file0
    assert ":" in file1 and "-" in file1 and "<NOT_FOUND" not in file1


def test_multi_node_writes_distinct_cv_per_node(ravendb_cluster, tmp_path):
    """Two independent nodes, same doc id -> two files with different CVs
    (each server has its own dbid).  File naming preserves node prefix."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]
    out = str(tmp_path / "doc-cv")

    write_doc(n1, "users/0", {"name": "on n1"})
    write_doc(n2, "users/0", {"name": "on n2"})

    diag.k_capture_doc_cv(
        params(nodes=[n1, n2], ids=["users/0"], output_dir=out))
    cv1 = open(os.path.join(out, f"{n1}__users_0.cv")).read().strip()
    cv2 = open(os.path.join(out, f"{n2}__users_0.cv")).read().strip()

    print(f"\n    expected: two files named with the node prefix; CVs differ")
    print(f"    actual:   {n1}__users_0.cv = {cv1!r}")
    print(f"              {n2}__users_0.cv = {cv2!r}\n")
    assert cv1 != ""
    assert cv2 != ""
    assert cv1 != cv2


def test_missing_doc_writes_not_found_sentinel(ravendb_cluster, tmp_path):
    """A doc that doesn't exist on the node => file contains the '<NOT_FOUND'
    sentinel.  Locks in the 'file set stays uniform' contract."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]
    out = str(tmp_path / "doc-cv")

    diag.k_capture_doc_cv(
        params(nodes=[node], ids=["users/missing"], output_dir=out))
    contents = open(os.path.join(out, f"{node}__users_missing.cv")).read().strip()

    print(f"\n    expected: file contains '<NOT_FOUND' sentinel")
    print(f"    actual:   {contents!r}\n")
    assert contents.startswith("<NOT_FOUND")


def test_unreachable_node_raises_loud(ravendb_cluster, tmp_path):
    """If a node is unreachable (connection error), raise loud instead of
    letting urllib.URLError fly up bare."""
    info = ravendb_cluster(n_nodes=1)  # spin a real server so the fixture
                                        # cleans up TARGET_URL_OVERRIDES
    out = str(tmp_path / "doc-cv")

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_capture_doc_cv(
            params(nodes=["dead-node"], ids=["users/0"], output_dir=out))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)
