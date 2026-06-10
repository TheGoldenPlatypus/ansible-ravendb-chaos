"""
Tests for k_cv_boundary_by_dbid.

What the kind does:  collects DatabaseId from each source node and from each
                     receiver node's /stats, then walks each receiver's
                     DatabaseChangeVector.  In the new-lane composite-CV form
                     ('order_side|stored_side'), FAILs if any entry on the
                     order side carries a source dbid.  With strict_v_new=True,
                     also FAILs any receiver still on the legacy raw CV form.
Returns:             list[str]
Raises:              ValueError if `source_nodes` or `receiver_nodes` is
                     missing.
                     RuntimeError if any source or receiver node is unreachable,
                     or if source/receiver dbid sets overlap.
                     DiagnosticViolation in assert_mode on a CV-boundary breach
                     (or on a legacy-form receiver when strict_v_new=True).
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def test_missing_source_or_receiver_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: source_nodes=None -> ValueError 'requires `source_nodes`'")
    with pytest.raises(ValueError, match="requires `source_nodes`") as exc1:
        diag.k_cv_boundary_by_dbid(params(source_nodes=None, receiver_nodes=[node]))
    print(f"    actual:   {exc1.value!s}")

    print(f"\n    expected: receiver_nodes=None -> ValueError 'requires `source_nodes`'")
    with pytest.raises(ValueError, match="requires `source_nodes`") as exc2:
        diag.k_cv_boundary_by_dbid(params(source_nodes=[node], receiver_nodes=None))
    print(f"    actual:   {exc2.value!s}\n")


def test_overlapping_dbids_raises(ravendb_cluster):
    """Pass the same node as both source and receiver -> identical DatabaseId
    in both sets -> RuntimeError 'overlap'."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning 'overlap'")
    with pytest.raises(RuntimeError, match="overlap") as exc:
        diag.k_cv_boundary_by_dbid(params(source_nodes=[node], receiver_nodes=[node]))
    print(f"    actual:   {exc.value!s}\n")


def test_legacy_cv_receivers_pass_when_strict_v_new_off(ravendb_cluster):
    """Real RavenDB writes legacy raw CVs (no '|') -> each receiver gets the
    'LEGACY raw CV' line; default strict_v_new=False -> PASS."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    src, rcv = info["nodes"]

    lines = diag.k_cv_boundary_by_dbid(params(
        source_nodes=[src], receiver_nodes=[rcv]))
    text = "\n".join(lines)

    print(f"\n    expected: 'LEGACY raw CV' line + 'PASS  no source dbids on any "
          f"receiver's order side'")
    print_lines("actual", lines)
    print()
    assert "LEGACY raw CV" in text
    assert "PASS  no source dbids on any receiver's order side" in text


def test_strict_v_new_fails_on_legacy_receivers(ravendb_cluster):
    """Same setup, but strict_v_new=True + assert_mode -> DiagnosticViolation
    on 'strict_v_new=true but legacy CV on'."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    src, rcv = info["nodes"]

    print(f"\n    expected: raises DiagnosticViolation with 'strict_v_new=true but legacy CV on'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_cv_boundary_by_dbid(params(
            source_nodes=[src], receiver_nodes=[rcv],
            strict_v_new=True, assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "strict_v_new=true but legacy CV on" in exc.value.lines[-1]


def test_source_dbid_on_receiver_order_side_fails(ravendb_cluster, monkeypatch):
    """Engineering composite CVs requires HashedRevisionPk+CompositeChangeVectors
    flags -- too heavy.  Monkeypatch get_stats to return source dbid 'SRC-1'
    and receiver CV 'A:1-SRC-1|B:2-RCV-1' (source dbid on order side)."""
    info = ravendb_cluster(n_nodes=1)
    src = info["nodes"][0]

    def fake_get_stats(p, target):
        if target == "src-node":
            return {"DatabaseId": "SRC-1", "DatabaseChangeVector": "A:1-SRC-1"}
        if target == "rcv-node":
            return {"DatabaseId": "RCV-1",
                    "DatabaseChangeVector": "A:1-SRC-1|B:2-RCV-1"}
        return None
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    lines = diag.k_cv_boundary_by_dbid(params(
        source_nodes=["src-node"], receiver_nodes=["rcv-node"]))
    text = "\n".join(lines)

    print(f"\n    expected: 'FAIL  CV-boundary breach: source dbid on order side'")
    print_lines("actual", lines)
    print()
    assert "FAIL  CV-boundary breach: source dbid on order side" in text
    assert "SRC-1" in text


def test_assert_mode_raises_on_source_leak(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)

    def fake_get_stats(p, target):
        if target == "src-node":
            return {"DatabaseId": "SRC-1", "DatabaseChangeVector": "A:1-SRC-1"}
        if target == "rcv-node":
            return {"DatabaseId": "RCV-1",
                    "DatabaseChangeVector": "A:1-SRC-1|B:2-RCV-1"}
        return None
    monkeypatch.setattr(diag, "get_stats", fake_get_stats)

    print(f"\n    expected: raises DiagnosticViolation with 'CV-boundary breach'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_cv_boundary_by_dbid(params(
            source_nodes=["src-node"], receiver_nodes=["rcv-node"],
            assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "CV-boundary breach" in exc.value.lines[-1]


def test_unreachable_source_raises_loud(ravendb_cluster):
    """Source unreachable -> can't enumerate source dbids -> RuntimeError.
    Distinct branch (not the 'overlap' or 'disjoint' umbrella) to eliminate
    false-positive 'sets overlap' messages when the real cause is offline."""
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up
    rcv = info["nodes"][0]

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES["dead-src"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'source nodes unreachable'")
    with pytest.raises(RuntimeError, match="source nodes unreachable") as exc:
        diag.k_cv_boundary_by_dbid(params(
            source_nodes=["dead-src"], receiver_nodes=[rcv]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-src" in str(exc.value)


def test_unreachable_receiver_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    src = info["nodes"][0]

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES["dead-rcv"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'receiver nodes unreachable'")
    with pytest.raises(RuntimeError, match="receiver nodes unreachable") as exc:
        diag.k_cv_boundary_by_dbid(params(
            source_nodes=[src], receiver_nodes=["dead-rcv"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-rcv" in str(exc.value)
