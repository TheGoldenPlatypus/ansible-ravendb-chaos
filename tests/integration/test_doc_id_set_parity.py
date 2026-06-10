"""
Tests for k_doc_id_set_parity.

What the kind does:  for each probe doc id, GETs it on every node.  PASSes if
                     every id is uniformly present (all 200) or uniformly
                     absent (all non-200).  Has a built-in retry/settle loop
                     for transient splits.
Returns:             list[str]
Raises:              ValueError if neither `ids` nor (`id_prefix` + `count`)
                     is given.
                     DiagnosticViolation in assert_mode on a split that
                     persists past retries.
                     RuntimeError if any node is unreachable.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_passes_when_all_ids_present_on_every_node(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    for i in range(3):
        write_doc(n1, f"users/{i}", {"i": i})
        write_doc(n2, f"users/{i}", {"i": i})

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], id_prefix="users", count=3))
    text = "\n".join(lines)

    print(f"\n    expected: contains 'PASS  every probe id is uniformly present or uniformly absent'")
    print_lines("actual", lines)
    print()
    assert "mismatched: 0" in text
    assert "PASS" in text


def test_passes_when_all_ids_absent_on_every_node(ravendb_cluster):
    """If we probe ids that exist on no node, the kind should still PASS:
    'uniformly absent' is a valid form of agreement."""
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], ids=["never-existed/0", "never-existed/1"]))
    text = "\n".join(lines)

    print(f"\n    expected: PASS even though both nodes returned 404 for every probe")
    print_lines("actual", lines)
    print()
    assert "mismatched: 0" in text
    assert "PASS" in text


def test_split_fails_when_id_present_on_one_node_only(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "only-on-n1"})    # n2 doesn't have it

    lines = diag.k_doc_id_set_parity(
        params(nodes=[n1, n2], ids=["users/0"]))         # info mode
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  doc-id-set split' and lists users/0")
    print_lines("actual", lines)
    print()
    assert "FAIL  doc-id-set split" in text
    assert "users/0" in text


def test_assert_mode_raises_on_persisted_split(ravendb_cluster):
    info = ravendb_cluster(n_nodes=2, cluster=False)
    n1, n2 = info["nodes"]

    write_doc(n1, "users/0", {"name": "only-on-n1"})

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  doc-id-set split'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_doc_id_set_parity(
            params(nodes=[n1, n2], ids=["users/0"], assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  doc-id-set split" in exc.value.lines[-1]


def test_unreachable_node_raises_loud(ravendb_cluster):
    """If ANY node can't be reached, the kind raises -- not silently treats
    'no response' as 'doc absent'."""
    info = ravendb_cluster(n_nodes=1)   # spin a real server so the fixture
                                         # cleans up TARGET_URL_OVERRIDES

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_doc_id_set_parity(
            params(nodes=["dead-node"], ids=["users/0"]))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)


def test_missing_ids_and_prefix_raises(ravendb_cluster):
    """Calling without `ids` and without (`id_prefix` + `count`) -> ValueError
    from expand_id_set."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `ids` OR'")
    with pytest.raises(ValueError, match="requires `ids` OR") as exc:
        diag.k_doc_id_set_parity(params(nodes=[node]))
    print(f"    actual:   {exc.value!s}\n")
