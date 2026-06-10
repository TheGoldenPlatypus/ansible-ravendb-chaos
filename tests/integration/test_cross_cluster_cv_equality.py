"""
Tests for k_cross_cluster_cv_equality.

What the kind does:  for each doc id, GETs the doc on every node and compares
                     the parsed @change-vector entries set-wise.  In default
                     mode requires equality across all nodes; in `anchor` mode
                     requires every other node's CV to be a superset of the
                     anchor's CV.
Returns:             list[str]
Raises:              ValueError if `nodes` has fewer than 2, if `doc_ids` is
                     empty, or if `anchor` isn't in `nodes`.
                     DiagnosticViolation in assert_mode on any MISMATCH.

Setup trick:         each test uses `ravendb_cluster(n_nodes=2, cluster=False)`
                     to spin TWO INDEPENDENT embedded servers (no joining).
                     Same doc id on each side -> each side generates its own
                     (dbid, etag) -> divergent CVs naturally.  Deterministic
                     FAIL paths without needing RAVEN_TEST_LICENSE.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import get_doc_cv, params, print_lines, write_doc


def test_raises_if_nodes_has_fewer_than_two():
    print(f"\n    expected: raises ValueError mentioning 'requires `nodes` with >=2'")
    with pytest.raises(ValueError, match="requires `nodes` with >=2") as exc:
        diag.k_cross_cluster_cv_equality(
            params(nodes=["1a"], doc_ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")


def test_raises_if_doc_ids_empty():
    print(f"\n    expected: raises ValueError mentioning 'requires `doc_ids`'")
    with pytest.raises(ValueError, match="requires `doc_ids`") as exc:
        diag.k_cross_cluster_cv_equality(
            params(nodes=["1a", "1b"], doc_ids=[]))
    print(f"    actual:   {exc.value!s}\n")


def test_raises_if_anchor_not_in_nodes():
    print(f"\n    expected: raises ValueError mentioning 'not in'")
    with pytest.raises(ValueError, match="not in") as exc:
        diag.k_cross_cluster_cv_equality(
            params(nodes=["1a", "1b"], doc_ids=["users/0"], anchor="2a"))
    print(f"    actual:   {exc.value!s}\n")


def test_equality_fails_when_two_independent_nodes_have_the_same_docid(ravendb_cluster):
    """Two independent servers, both have a doc with id 'users/0', but each
    server generated its own (dbid, etag) -> divergent CVs -> MISMATCH."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "Alice on n1"})
    write_doc(n2, "users/0", {"name": "Alice on n2"})

    cv1 = get_doc_cv(n1, "users/0")
    cv2 = get_doc_cv(n2, "users/0")

    lines = diag.k_cross_cluster_cv_equality(
        params(nodes=[n1, n2], doc_ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    n1 cv:    {cv1}")
    print(f"    n2 cv:    {cv2}")
    print(f"    expected: text contains 'MISMATCH' and last line contains 'FAIL'")
    print_lines("actual", lines)
    print()
    assert "MISMATCH" in text
    assert "FAIL" in lines[-1]


def test_assert_mode_raises_diagnostic_violation_on_mismatch(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"v": 1})
    write_doc(n2, "users/0", {"v": 1})

    print(f"\n    expected: raises DiagnosticViolation with 'MISMATCH' in lines")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_cross_cluster_cv_equality(
            params(nodes=[n1, n2], doc_ids=["users/0"], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "MISMATCH" in "\n".join(exc.value.lines)


def test_doc_missing_on_one_node_is_reported_as_unreachable_http_404(ravendb_cluster):
    """Real RavenDB returns HTTP 404 for missing-id GETs (not 200-with-empty),
    so the kind reports 'HTTP_404' and tallies the node as UNREACHABLE."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "only on n1"})

    lines = diag.k_cross_cluster_cv_equality(
        params(nodes=[n1, n2], doc_ids=["users/0"]))
    text = "\n".join(lines)

    print(f"\n    expected: text contains 'UNREACHABLE' + 'HTTP_404'; last line is FAIL")
    print_lines("actual", lines)
    print()
    assert "UNREACHABLE" in text
    assert "HTTP_404" in text
    assert "FAIL" in lines[-1]


def test_checks_every_doc_id_even_if_first_mismatches(ravendb_cluster):
    """Counters in the summary line reflect ALL docs, not just the first one."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"v": 1})
    write_doc(n2, "users/0", {"v": 2})
    write_doc(n1, "users/1", {"v": 1})
    write_doc(n2, "users/1", {"v": 1})

    lines = diag.k_cross_cluster_cv_equality(
        params(nodes=[n1, n2], doc_ids=["users/0", "users/1"]))
    summary = lines[-2]

    print(f"\n    expected: summary 'checked 2 doc(s); mismatched=2 unreachable=0'")
    print(f"    actual:   {summary!r}\n")
    assert "checked 2 doc(s); mismatched=2  unreachable=0" in summary


def test_anchor_mode_fails_when_anchor_entries_absent_from_other(ravendb_cluster):
    """Anchor=n1.  n2's CV doesn't contain n1's (dbid,etag) entries -> subset
    check FAILS, and the anchor row is marked with '<-- anchor'."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"v": 1})
    write_doc(n2, "users/0", {"v": 1})

    lines = diag.k_cross_cluster_cv_equality(
        params(nodes=[n1, n2], doc_ids=["users/0"], anchor=n1))
    text = "\n".join(lines)

    print(f"\n    expected: text contains 'MISMATCH' and '<-- anchor' marker")
    print_lines("actual", lines)
    print()
    assert "MISMATCH" in text
    assert "<-- anchor" in text
