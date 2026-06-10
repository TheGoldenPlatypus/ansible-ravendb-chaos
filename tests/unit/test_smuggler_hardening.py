"""
Tests for the smuggler hardening.

What the module does:  k_export POSTs /databases/<db>/smuggler/export and writes
                       the .ravendbdump body to dump_path; k_import POSTs the
                       same file multipart-uploaded.
Hardening focus:       (1) k_import no longer accepts skip_if_missing -- a
                       missing dump file is a HARD failure now.  Scenarios
                       that need to no-op on absence must gate with `when:`
                       at the playbook layer.
                       (2) k_import refuses to upload a 0-byte file (would
                       silently pass without importing anything).
                       (3) k_export refuses to write a 0-byte response file
                       (would silently produce a useless dump).

These are unit tests: ravendb_smuggler.request is monkeypatched.
"""

import os
from collections import defaultdict

import pytest

import ravendb_smuggler as smuggler


def params(**kwargs):
    p = defaultdict(lambda: None)
    p["db_name"] = "db1"
    p["ravendb_domain"] = "ignored-in-unit-tests"
    p["client_cert"] = ""
    p["ca_cert"] = ""
    p.update(kwargs)
    return p


# ---- k_export --------------------------------------------------------------

def test_export_raises_on_empty_body(monkeypatch, tmp_path):
    """Server returned HTTP 200 but with 0 bytes -- writing that to disk would
    produce a dump file that 'imports' as a vacuous no-op."""
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, b""
    monkeypatch.setattr(smuggler, "request", fake)

    dump = str(tmp_path / "out.ravendbdump")
    print(f"\n    expected: RuntimeError mentioning 'empty body' / 'refusing'")
    with pytest.raises(RuntimeError, match="empty body") as exc:
        smuggler.k_export(params(target="1a", dump_path=dump))
    print(f"    actual:   {exc.value!s}")
    assert not os.path.exists(dump), "must not write a 0-byte dump file"
    print(f"\n    file written:   {os.path.exists(dump)} (must be False)\n")


def test_export_raises_on_http_non_200(monkeypatch, tmp_path):
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 500, b'{"error":"fake"}'
    monkeypatch.setattr(smuggler, "request", fake)

    dump = str(tmp_path / "out.ravendbdump")
    print(f"\n    expected: RuntimeError mentioning 'HTTP 500'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        smuggler.k_export(params(target="1a", dump_path=dump))
    print(f"    actual:   {exc.value!s}\n")


def test_export_writes_real_dump_on_happy_path(monkeypatch, tmp_path):
    fake_dump = b"FAKE_RAVENDB_DUMP_CONTENTS"
    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, fake_dump
    monkeypatch.setattr(smuggler, "request", fake)

    dump = str(tmp_path / "out.ravendbdump")
    msg = smuggler.k_export(params(target="1a", dump_path=dump))
    print(f"\n    expected: file exists at {dump} with {len(fake_dump)} bytes")
    print(f"    actual:   {os.path.getsize(dump)} bytes  msg={msg!r}\n")
    assert os.path.exists(dump)
    assert os.path.getsize(dump) == len(fake_dump)


# ---- k_import --------------------------------------------------------------

def test_import_raises_on_missing_file(tmp_path):
    """The skip_if_missing escape hatch is gone.  Absence MUST fail loud."""
    missing = str(tmp_path / "does-not-exist.ravendbdump")
    print(f"\n    expected: FileNotFoundError mentioning 'dump file not found'")
    with pytest.raises(FileNotFoundError, match="dump file not found") as exc:
        smuggler.k_import(params(target="1a", dump_path=missing))
    print(f"    actual:   {exc.value!s}\n")


def test_import_raises_on_zero_byte_file(tmp_path):
    """0-byte dump must NOT be uploaded -- would silently 'import' nothing."""
    dump = str(tmp_path / "empty.ravendbdump")
    open(dump, "wb").close()                  # touch zero-byte file

    print(f"\n    expected: RuntimeError mentioning '0 bytes' / 'empty payload'")
    with pytest.raises(RuntimeError, match="0 bytes") as exc:
        smuggler.k_import(params(target="1a", dump_path=dump))
    print(f"    actual:   {exc.value!s}\n")


def test_import_raises_on_http_non_200(monkeypatch, tmp_path):
    dump = str(tmp_path / "fake.ravendbdump")
    with open(dump, "wb") as f:
        f.write(b"NON_EMPTY_DUMP_CONTENTS")

    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 500, b'{"error":"server bad"}'
    monkeypatch.setattr(smuggler, "request", fake)

    print(f"\n    expected: RuntimeError mentioning 'HTTP 500'")
    with pytest.raises(RuntimeError, match="HTTP 500") as exc:
        smuggler.k_import(params(target="1a", dump_path=dump))
    print(f"    actual:   {exc.value!s}\n")


def test_import_happy_path_reports_size(monkeypatch, tmp_path):
    dump = str(tmp_path / "fake.ravendbdump")
    payload = b"BIG_FAKE_DUMP" * 100
    with open(dump, "wb") as f:
        f.write(payload)

    def fake(method, target, domain, path, client_cert, ca_cert,
             body=None, content_type=None, timeout=30):
        return 200, b'{"OperationId":7}'
    monkeypatch.setattr(smuggler, "request", fake)

    msg = smuggler.k_import(params(target="1a", dump_path=dump))
    print(f"\n    expected: 'IMPORTED ... ({len(payload)} bytes, HTTP 200)'")
    print(f"    actual:   {msg!r}\n")
    assert "IMPORTED" in msg
    assert f"{len(payload)} bytes" in msg
