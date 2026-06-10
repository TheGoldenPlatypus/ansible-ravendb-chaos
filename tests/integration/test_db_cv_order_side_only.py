"""
Tests for k_db_cv_order_side_only.

What the kind does:  reads each receiver node's database change vector and
                     checks every CV entry's cluster tag belongs to the
                     receiver's own cluster -- no foreign tags from other
                     clusters.  Allowed tags default to the trailing letter
                     of each receiver node name ('1a' -> 'A'); pass
                     `receiver_group_tags` to override.
Returns:             list[str]
Raises:              ValueError if `receiver_group_nodes` is missing.
                     RuntimeError if any receiver is unreachable, or if any
                     receiver reports an empty DatabaseChangeVector (vacuous
                     PASS would be a silent false negative).
                     DiagnosticViolation in assert_mode on any foreign tag.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_missing_receiver_group_nodes_raises(ravendb_cluster):
    print(f"\n    expected: raises ValueError mentioning 'requires `receiver_group_nodes`'")
    with pytest.raises(ValueError, match="requires `receiver_group_nodes`") as exc:
        diag.k_db_cv_order_side_only(params(receiver_group_nodes=None))
    print(f"    actual:   {exc.value!s}\n")


def test_real_single_node_passes_with_auto_tag(ravendb_cluster):
    """One real node '1a'.  Write a doc so the CV gets populated with the
    server's cluster tag ('A').  Auto-derived allowed tags = ['A'].  PASS."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    write_doc(node, "users/0", {"v": 1})

    lines = diag.k_db_cv_order_side_only(params(
        receiver_group_nodes=[node], assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'every receiver's DB CV references only [...]'")
    print_lines("actual", lines)
    print()
    assert "PASS  every receiver's DB CV references only" in text
    assert "LEAK" not in text


def test_foreign_tag_fails_via_monkeypatch(ravendb_cluster, monkeypatch):
    """Engineering a real foreign-tag CV needs a multi-cluster setup.
    Monkeypatch get_stats so receiver returns CV with tag 'C' -> info-mode
    FAIL line."""
    info = ravendb_cluster(n_nodes=1)
    rcv = info["nodes"][0]

    def fake_get_stats(p, target):
        return {"DatabaseChangeVector": "C:1-xxx"}
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    lines = diag.k_db_cv_order_side_only(params(
        receiver_group_nodes=[rcv]))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  CV-boundary breach: foreign tag on' + 'LEAK'")
    print_lines("actual", lines)
    print()
    assert "FAIL  CV-boundary breach: foreign tag on" in text
    assert "LEAK" in text


def test_assert_mode_raises_on_foreign_tag(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)
    rcv = info["nodes"][0]

    def fake_get_stats(p, target):
        return {"DatabaseChangeVector": "C:1-xxx"}
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    print(f"\n    expected: raises DiagnosticViolation with 'CV-boundary breach'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_db_cv_order_side_only(params(
            receiver_group_nodes=[rcv], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "CV-boundary breach" in exc.value.lines[-1]


def test_explicit_tags_override_auto_derivation(ravendb_cluster, monkeypatch):
    """Receiver node '1a' would auto-derive allowed tags=['A'].  With CV='X:1-xxx'
    that would FAIL.  But passing receiver_group_tags=['X'] makes 'X' allowed
    and the test passes."""
    info = ravendb_cluster(n_nodes=1)
    rcv = info["nodes"][0]

    def fake_get_stats(p, target):
        return {"DatabaseChangeVector": "X:1-xxx"}
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    lines = diag.k_db_cv_order_side_only(params(
        receiver_group_nodes=[rcv],
        receiver_group_tags=["X"],
        assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS (explicit tags=['X'] override auto 'A')")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "FAIL" not in text


def test_empty_cv_receiver_raises_loud(ravendb_cluster, monkeypatch):
    """Receiver is reachable but its DatabaseChangeVector is empty (e.g. brand-
    new DB before any replication landed).  0 entries -> nothing to check ->
    vacuous PASS would be a silent false negative.  Must fail loud."""
    info = ravendb_cluster(n_nodes=1)
    rcv = info["nodes"][0]

    def fake_get_stats(p, target):
        return {"DatabaseChangeVector": ""}
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    print(f"\n    expected: raises RuntimeError mentioning 'empty DatabaseChangeVector'")
    with pytest.raises(RuntimeError, match="empty DatabaseChangeVector") as exc:
        diag.k_db_cv_order_side_only(params(receiver_group_nodes=[rcv]))
    print(f"    actual:   {exc.value!s}\n")


def test_unreachable_receiver_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES["dead-rcv"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'receiver nodes unreachable'")
    with pytest.raises(RuntimeError, match="receiver nodes unreachable") as exc:
        diag.k_db_cv_order_side_only(params(
            receiver_group_nodes=["dead-rcv"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-rcv" in str(exc.value)
