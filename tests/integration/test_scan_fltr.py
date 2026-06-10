"""
Tests for k_scan_fltr.

What the kind does:  walks `capture_dir` recursively for *.cv files; FAILs on
                     any file whose content contains the 'FLTR:' marker.
                     Pure filesystem -- no HTTP, no RavenDB.
Returns:             list[str]
Raises:              ValueError if `capture_dir` is missing or not a directory.
                     RuntimeError if 0 .cv files were found (vacuous PASS would
                     be a silent false negative).
                     DiagnosticViolation in assert_mode on any FLTR leak.
"""

import os

import pytest

import ravendb_diagnostic as diag
from raven_lab import params, print_lines


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def test_clean_cv_files_pass(tmp_path):
    _write(str(tmp_path / "a.cv"), "A:1-aaa, B:2-bbb")
    _write(str(tmp_path / "b.cv"), "C:3-ccc")

    lines = diag.k_scan_fltr(params(capture_dir=str(tmp_path), assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: 'FLTR scan: 2 .cv file(s) ... leaks=0', no LEAK/FAIL")
    print_lines("actual", lines)
    print()
    assert "leaks=0" in text
    assert "2 .cv file(s)" in text
    assert "LEAK" not in text
    assert "FAIL" not in text


def test_leak_detected_in_info_mode(tmp_path):
    """One .cv carries the FLTR: marker -> LEAK line, but info mode returns
    normally (no DiagnosticViolation)."""
    _write(str(tmp_path / "clean.cv"), "A:1-aaa")
    _write(str(tmp_path / "dirty.cv"), "FLTR: blocked-by-filter")

    lines = diag.k_scan_fltr(params(capture_dir=str(tmp_path)))   # info mode
    text = "\n".join(lines)

    print(f"\n    expected: 'leaks=1' + a 'LEAK' line mentioning dirty.cv")
    print_lines("actual", lines)
    print()
    assert "leaks=1" in text
    assert "LEAK" in text
    assert "dirty.cv" in text


def test_assert_mode_raises_on_leak(tmp_path):
    _write(str(tmp_path / "dirty.cv"), "FLTR: leaked-cv-here")

    print(f"\n    expected: raises DiagnosticViolation with 'FAIL  FLTR leakage detected'")
    with pytest.raises(diag.DiagnosticViolation) as exc:
        diag.k_scan_fltr(params(capture_dir=str(tmp_path), assert_mode=True))
    print(f"    actual:   last line = {exc.value.lines[-1]!r}\n")
    assert "FAIL  FLTR leakage detected" in exc.value.lines[-1]


def test_nested_subdirs_scanned(tmp_path):
    """os.walk recurses -- a .cv buried in subdirs is still found."""
    nested = str(tmp_path / "sub" / "deeper" / "leaky.cv")
    _write(nested, "FLTR: deep-leak")

    lines = diag.k_scan_fltr(params(capture_dir=str(tmp_path)))
    text = "\n".join(lines)

    print(f"\n    expected: leak found in nested path 'sub/deeper/leaky.cv'")
    print_lines("actual", lines)
    print()
    assert "leaks=1" in text
    assert "leaky.cv" in text


def test_non_cv_files_are_ignored(tmp_path):
    """Only *.cv files are scanned; notes.txt with FLTR: must NOT trigger."""
    _write(str(tmp_path / "a.cv"), "A:1-aaa")              # clean .cv
    _write(str(tmp_path / "notes.txt"), "FLTR: ignore-me") # wrong extension

    lines = diag.k_scan_fltr(params(capture_dir=str(tmp_path), assert_mode=True))
    text = "\n".join(lines)

    print(f"\n    expected: 'leaks=0' (notes.txt ignored)")
    print_lines("actual", lines)
    print()
    assert "1 .cv file(s)" in text
    assert "leaks=0" in text


def test_missing_capture_dir_raises(tmp_path):
    print(f"\n    expected: capture_dir=None -> ValueError 'existing directory'")
    with pytest.raises(ValueError, match="existing directory") as exc1:
        diag.k_scan_fltr(params(capture_dir=None))
    print(f"    actual:   {exc1.value!s}")

    bogus = str(tmp_path / "does-not-exist")
    print(f"\n    expected: capture_dir={bogus!r} -> ValueError 'existing directory'")
    with pytest.raises(ValueError, match="existing directory") as exc2:
        diag.k_scan_fltr(params(capture_dir=bogus))
    print(f"    actual:   {exc2.value!s}\n")


def test_empty_dir_raises_loud(tmp_path):
    """0 .cv files -> nothing to scan -> can't verify FLTR-cleanliness.
    Vacuous PASS would be a silent false negative."""
    print(f"\n    expected: raises RuntimeError mentioning '0 .cv files found'")
    with pytest.raises(RuntimeError, match="0 .cv files found") as exc:
        diag.k_scan_fltr(params(capture_dir=str(tmp_path)))
    print(f"    actual:   {exc.value!s}\n")
