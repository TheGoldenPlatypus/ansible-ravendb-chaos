"""
Tests for k_lane_inert.

What the kind does:  samples N revisions per id_prefix per node.  FAILs if any
                     revision's @change-vector contains '|' (the composite-CV
                     delimiter from the new lane).  Used to confirm that during
                     a rolling upgrade the new lane stayed inert on the legacy
                     side.
Returns:             list[str]
Raises:              ValueError if `id_prefixes` is missing.
                     DiagnosticViolation in assert_mode on any leak.
                     RuntimeError if any node is unreachable.
"""

import pytest

import ravendb_diagnostic as diag
from raven_lab import enable_revisions, params, print_lines, write_doc


def test_empty_db_raises_loud(ravendb_cluster):
    """No revisions sampled -> can't verify lane-inertness -> fail loud.
    A vacuous PASS here would be a silent false negative."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises RuntimeError mentioning '0 revisions sampled'")
    with pytest.raises(RuntimeError, match="0 revisions sampled") as exc:
        diag.k_lane_inert(params(
            nodes=[node], id_prefixes=["users"], sample_per_prefix=3,
        ))
    print(f"    actual:   {exc.value!s}\n")


def test_legacy_cv_revs_have_no_pipe(ravendb_cluster):
    """Default RavenDB writes legacy raw CVs (no '|') -> PASS on real data."""
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    enable_revisions(node)
    for v in range(3):
        write_doc(node, "users/0", {"v": v})

    lines = diag.k_lane_inert(params(
        nodes=[node], id_prefixes=["users"], sample_per_prefix=3,
    ))
    text = "\n".join(lines)

    print(f"\n    expected: 'PASS' with revs actually sampled (sampled > 0)")
    print_lines("actual", lines)
    print()
    assert "PASS" in text
    assert "leaks (new-lane '|' in CV): 0" in text
    # sampled must be > 0 (real revs read), not the empty-db vacuous case.
    assert "0 revisions sampled" not in text


def test_missing_id_prefixes_raises(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    print(f"\n    expected: raises ValueError mentioning 'requires `id_prefixes`'")
    with pytest.raises(ValueError, match="requires `id_prefixes`") as exc:
        diag.k_lane_inert(params(nodes=[node], id_prefixes=None))
    print(f"    actual:   {exc.value!s}\n")


def test_unreachable_node_raises_loud(ravendb_cluster):
    info = ravendb_cluster(n_nodes=1)   # real server so the fixture cleans up

    import ravendb_client
    ravendb_client.TARGET_URL_OVERRIDES.clear()
    ravendb_client.TARGET_URL_OVERRIDES["dead-node"] = "http://127.0.0.1:1"

    print(f"\n    expected: raises RuntimeError mentioning 'unreachable'")
    with pytest.raises(RuntimeError, match="unreachable") as exc:
        diag.k_lane_inert(params(
            nodes=["dead-node"], id_prefixes=["users"], sample_per_prefix=3,
        ))
    print(f"    actual:   {exc.value!s}\n")
    assert "dead-node" in str(exc.value)


def _fake_request_with_pipe_in_cv(target, doc_id):
    """Build a /revisions response with one rev whose CV contains '|'."""
    import json
    body = json.dumps({
        "Results": [{
            "@metadata": {"@change-vector": "A:1-aaa|B:2-bbb"},
        }],
    }).encode()
    return 200, body


def test_leak_path_fails_when_revision_cv_contains_pipe(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        if "/revisions?" in path:
            return _fake_request_with_pipe_in_cv(target, path)
        if "/admin/databases" in path:        # SupportedFeatures forensic
            import json
            return 200, json.dumps({"SupportedFeatures": []}).encode()
        return 404, b""
    monkeypatch.setattr(diag, "request", fake_request)

    lines = diag.k_lane_inert(params(
        nodes=[node], id_prefixes=["users"], sample_per_prefix=1,
    ))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: contains 'FAIL  lane-inert breach' (composite CV detected)")
    print_lines("actual", lines)
    print()
    assert "FAIL  lane-inert breach" in text
    assert "A:1-aaa|B:2-bbb" in text


def test_leak_path_raises_in_assert_mode(ravendb_cluster, monkeypatch):
    info = ravendb_cluster(n_nodes=1)
    node = info["nodes"][0]

    def fake_request(method, target, domain, path, client_cert, ca_cert,
                     body=None, content_type=None, timeout=30):
        if "/revisions?" in path:
            return _fake_request_with_pipe_in_cv(target, path)
        if "/admin/databases" in path:
            import json
            return 200, json.dumps({"SupportedFeatures": []}).encode()
        return 404, b""
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  lane-inert breach'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_lane_inert(params(
            nodes=[node], id_prefixes=["users"], sample_per_prefix=1,
            assert_mode=True,
        ))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  lane-inert breach" in exc.value.lines[-1]
