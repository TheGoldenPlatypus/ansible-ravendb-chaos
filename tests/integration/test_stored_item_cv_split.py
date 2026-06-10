"""
Tests for k_stored_item_cv_split.

What the kind does:  on one node, GETs each probe doc and inspects its
                     @change-vector.  With expect='raw' every CV must be the
                     legacy form (no delimiter).  With expect='split' every CV
                     must contain `delimiter` (default '|', the composite-CV
                     marker).  Special case: expect='split' but NO probe doc
                     has the delimiter -> N/A (build's composite lane isn't
                     active), not FAIL.
Returns:             list[str]
Raises:              ValueError if `doc_ids` is missing.
                     RuntimeError if the target is unreachable, or if any
                     probe doc returns non-200 (partial coverage would be a
                     vacuous PASS).
                     DiagnosticViolation in assert_mode on any shape mismatch.
"""

import json

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines, write_doc


def test_missing_doc_ids_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `doc_ids`'")
    with pytest.raises(ValueError, match="requires `doc_ids`") as exc:
        diag.k_stored_item_cv_split(params(target=node, doc_ids=[]))
    print(f"    actual:   {exc.value!s}\n")


def test_expect_raw_passes_on_real_docs(ravendb_cluster):
    """Real RavenDB writes legacy raw CVs (no '|').  expect='raw' -> PASS."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    for i in range(3):
        write_doc(node, f"users/{i}", {"i": i})

    lines = diag.k_stored_item_cv_split(params(
        target=node,
        doc_ids=["users/0", "users/1", "users/2"],
        expect="raw",
        assert_mode=True,
    ))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'every probed doc matches expected raw shape'")
    print_lines("actual", lines)
    print()
    assert "PASS  every probed doc matches expected 'raw' shape" in text
    assert "MISMATCH" not in text


def test_expect_split_returns_na_on_legacy_build(ravendb_cluster):
    """Legacy CVs, expect='split' -> every doc gets 'LEGACY raw CV' + final
    'N/A' line (composite-CV lane not active).  Not a FAIL."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    for i in range(2):
        write_doc(node, f"users/{i}", {"i": i})

    lines = diag.k_stored_item_cv_split(params(
        target=node,
        doc_ids=["users/0", "users/1"],
        expect="split",
    ))
    text = "\n".join(lines)

    print(f"\n    expected: 'LEGACY raw CV' + 'N/A  legacy raw-CV form'; no FAIL")
    print_lines("actual", lines)
    print()
    assert "LEGACY raw CV" in text
    assert "N/A  legacy raw-CV form across all probes" in text
    assert "FAIL" not in text


def test_expect_raw_fails_on_composite_via_monkeypatch(ravendb_cluster, monkeypatch):
    """Engineering composite CVs needs heavy feature flags.  Monkeypatch
    diag.request so docs return CVs with '|'.  expect='raw' -> MISMATCH + FAIL."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        return 200, json.dumps({
            "Results": [{
                "@metadata": {"@change-vector": "A:1-aaa|B:2-bbb"},
            }],
        }).encode()
    monkeypatch.setattr(diag, "request", fake_request)

    lines = diag.k_stored_item_cv_split(params(
        target=node, doc_ids=["users/0"], expect="raw"))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: 'MISMATCH' + 'FAIL  1 doc(s) don't match expected shape'")
    print_lines("actual", lines)
    print()
    assert "MISMATCH" in text
    assert "FAIL  1 doc(s) don't match expected shape" in text


def test_assert_mode_raises_on_mismatch(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        return 200, json.dumps({
            "Results": [{
                "@metadata": {"@change-vector": "A:1-aaa|B:2-bbb"},
            }],
        }).encode()
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: raises DiagnosticViolation with \"don't match expected shape\"")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_stored_item_cv_split(params(
            target=node, doc_ids=["users/0"], expect="raw", assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "users/0" in exc.value.lines[-1]


def test_expect_split_passes_on_composite_via_monkeypatch(ravendb_cluster, monkeypatch):
    """All docs return composite CVs containing '|'.  expect='split' -> PASS."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        return 200, json.dumps({
            "Results": [{
                "@metadata": {"@change-vector": "A:1-aaa|B:2-bbb"},
            }],
        }).encode()
    monkeypatch.setattr(diag, "request", fake_request)

    lines = diag.k_stored_item_cv_split(params(
        target=node, doc_ids=["users/0", "users/1"],
        expect="split", assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS 'every probed doc matches expected split shape'")
    print_lines("actual", lines)
    print()
    assert "PASS  every probed doc matches expected 'split' shape" in text


def test_partial_unreadable_raises_loud(ravendb_cluster):
    """Probe one real doc + one missing id.  The 404 must NOT be silently
    skipped -- partial coverage = vacuous PASS risk.  Raises loud."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    write_doc(node, "users/0", {"v": 1})

    print(f"\n    expected: raises RuntimeError mentioning 'probe doc(s) unreadable'")
    with pytest.raises(RuntimeError, match="probe doc\\(s\\) unreadable") as exc:
        diag.k_stored_item_cv_split(params(
            target=node, doc_ids=["users/0", "users/missing"], expect="raw"))
    print(f"    actual:   {exc.value!s}\n")
    assert "users/missing" in str(exc.value)


def test_unreachable_target_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES["dead-target"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'target ... unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_stored_item_cv_split(params(
            target="dead-target", doc_ids=["users/0"], expect="raw"))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-target" in str(exc.value)
