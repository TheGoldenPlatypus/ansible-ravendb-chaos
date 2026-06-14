"""
Tests for k_revision_form_sampling.

What the kind does:  for each probe doc id, GETs /revisions?id=<id>&pageSize=N
                     on a single node and classifies every returned revision's
                     @change-vector by whether it contains the configured
                     delimiter (default '|'):
                       raw   -- no delimiter      (legacy / pre-hashed)
                       split -- has the delimiter (new lane / hashed)
                     Aggregates per-id counts + totals.  Modes via `expect`:
                       any-raw   -- >=1 raw revision (post-snapshot restore)
                       all-split -- every revision split (post-smuggler)
                       any-split / all-raw -- symmetric variants
                     Probe ids that return zero revisions are flagged
                     `missing` and FAIL under assert_mode.

Pinned here:         expect-mode assertions PASS / FAIL as documented;
                     missing-revisions ids are caught loudly;
                     non-200 HTTP raises RuntimeError;
                     invalid `expect` raises ValueError.
"""

import json
from collections import defaultdict

import pytest

import ravendb_diagnostic as diag


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


def _revs_body(cvs):
    """Build a 200 response body with one Results entry per CV string."""
    return json.dumps({
        "Results": [{"@metadata": {"@change-vector": cv}} for cv in cvs]
    }).encode()


def _id_from_path(path):
    return path.split("id=", 1)[1].split("&", 1)[0]


def _build_request(per_id):
    """Build a fake `request()` returning the configured CV list per doc id.
    `per_id` is a dict {doc_id: [cv_str, ...]}.  Doc ids not in the map
    return an empty Results array (the 'missing' case)."""
    def fake_request(method, target, domain, path, *a, **kw):
        if path.startswith("/databases/db1/revisions?"):
            doc_id = _id_from_path(path)
            return 200, _revs_body(per_id.get(doc_id, []))
        raise AssertionError("unexpected path %r" % path)
    return fake_request


# ---- happy paths ---------------------------------------------------------

def test_passes_any_raw_when_at_least_one_revision_is_raw(monkeypatch):
    """Snapshot-restore case: mixed-form revisions on disk -> at least one raw."""
    per_id = {
        "users/sink1/0": ["A:1-dbidA", "A:2-dbidA|B:1-dbidB"],   # 1 raw + 1 split
        "users/sink1/1": ["A:1-dbidA|B:1-dbidB", "A:2-dbidA|B:2-dbidB"],   # both split
    }
    monkeypatch.setattr(diag, "request", _build_request(per_id))

    lines = diag.k_revision_form_sampling(
        params(target="3a", ids=list(per_id), expect="any-raw", assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS, totals raw=1 split=3")
    for ln in lines: print(f"        {ln}")
    print()
    assert "PASS  expect=any-raw" in text
    assert "raw=1  split=3" in text


def test_passes_all_split_when_every_revision_is_split(monkeypatch):
    """Smuggler-restore case: every revision was re-keyed -> all split."""
    per_id = {
        "users/sink1/0": ["A:1-dbidA|B:1-dbidB", "A:2-dbidA|B:2-dbidB"],
        "users/sink1/1": ["A:3-dbidA|B:3-dbidB"],
    }
    monkeypatch.setattr(diag, "request", _build_request(per_id))

    lines = diag.k_revision_form_sampling(
        params(target="4a", ids=list(per_id), expect="all-split", assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: PASS expect=all-split, raw=0 split=3")
    for ln in lines: print(f"        {ln}")
    print()
    assert "PASS  expect=all-split" in text
    assert "raw=0  split=3" in text


# ---- failure paths -------------------------------------------------------

def test_fails_any_raw_when_no_revision_is_raw(monkeypatch):
    """Snapshot restore that lost raw form -- the bug case."""
    per_id = {
        "users/sink1/0": ["A:1|B:1", "A:2|B:2"],   # all split
        "users/sink1/1": ["A:3|B:3"],
    }
    monkeypatch.setattr(diag, "request", _build_request(per_id))

    print(f"\n    expected: DiagnosticViolation -- 'expected any-raw but every revision is split'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_revision_form_sampling(
            params(target="3a", ids=list(per_id), expect="any-raw", assert_mode=True))
    last = exc.value.lines[-1]
    print(f"    actual:   {last!r}\n")
    assert "FAIL" in last
    assert "any-raw" in last


def test_fails_all_split_when_a_revision_is_raw(monkeypatch):
    """Smuggler restore that left a raw revision behind -- the bug case."""
    per_id = {
        "users/sink1/0": ["A:1-X", "A:2|B:2"],   # 1 raw slipped through
    }
    monkeypatch.setattr(diag, "request", _build_request(per_id))

    print(f"\n    expected: DiagnosticViolation -- 'expected all-split but 1 revision(s) are raw'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_revision_form_sampling(
            params(target="4a", ids=list(per_id), expect="all-split", assert_mode=True))
    last = exc.value.lines[-1]
    print(f"    actual:   {last!r}\n")
    assert "FAIL" in last
    assert "1 revision(s) are raw" in last


def test_fails_loud_when_an_id_returns_no_revisions(monkeypatch):
    """Doc id with zero revs -> 'missing' bucket -> FAIL under assert_mode.
    Old design would have silently dropped the id and PASSed the rest."""
    per_id = {
        "users/sink1/0": ["A:1|B:1"],
        "users/sink1/1": [],   # missing -- the smuggler dropped this doc
    }
    monkeypatch.setattr(diag, "request", _build_request(per_id))

    print(f"\n    expected: DiagnosticViolation mentioning 'returned no revisions'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_revision_form_sampling(
            params(target="4a", ids=list(per_id), expect="all-split", assert_mode=True))
    last = exc.value.lines[-1]
    print(f"    actual:   {last!r}\n")
    assert "returned no revisions" in last
    assert "users/sink1/1" in last


# ---- input guards --------------------------------------------------------

def test_raises_on_unknown_expect(monkeypatch):
    monkeypatch.setattr(diag, "request", _build_request({"u/0": ["A:1"]}))

    print(f"\n    expected: ValueError listing the allowed `expect` values")
    with pytest.raises(ValueError, match="any-raw") as exc:
        diag.k_revision_form_sampling(
            params(target="3a", ids=["u/0"], expect="something-else"))
    print(f"    actual:   {exc.value!s}\n")


def test_raises_loud_on_non_200(monkeypatch):
    def fake_request(method, target, domain, path, *a, **kw):
        return 404, b""
    monkeypatch.setattr(diag, "request", fake_request)

    print(f"\n    expected: RuntimeError mentioning 'db missing or node unreachable'")
    with pytest.raises(RuntimeError, match="db missing or node unreachable") as exc:
        diag.k_revision_form_sampling(
            params(target="3a", ids=["u/0"], expect="any-raw"))
    print(f"    actual:   {exc.value!s}\n")
