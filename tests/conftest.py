import ast
import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
for p in (REPO_ROOT, REPO_ROOT / "library", REPO_ROOT / "module_utils"):
    sys.path.insert(0, str(p))


def _install_ansible_shim():
    if "ansible.module_utils.ravendb_client" in sys.modules:
        return

    if "ansible" not in sys.modules:
        sys.modules["ansible"] = types.ModuleType("ansible")
    if "ansible.module_utils" not in sys.modules:
        mu = types.ModuleType("ansible.module_utils")
        sys.modules["ansible.module_utils"] = mu
        sys.modules["ansible"].module_utils = mu

    def _install(util_name):
        """Load module_utils/<util_name>.py under both `ansible.module_utils.<name>`
        and bare `<name>` so kinds + tests see the same module object."""
        spec = importlib.util.spec_from_file_location(
            "ansible.module_utils." + util_name,
            str(REPO_ROOT / "module_utils" / (util_name + ".py")),
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        sys.modules["ansible.module_utils." + util_name] = m
        setattr(sys.modules["ansible.module_utils"], util_name, m)
        sys.modules[util_name] = m
        return m

    mod = _install("ravendb_client")
    _install("polling")

    # Minimal AnsibleModule stub -- diagnostic imports it but tests call kinds
    # directly, not through main(), so this rarely matters.
    if "ansible.module_utils.basic" not in sys.modules:
        basic = types.ModuleType("ansible.module_utils.basic")
        class _StubModule:
            def __init__(self, argument_spec=None, **kw):
                self.params = {}
            def fail_json(self, **kw): raise SystemExit(kw)
            def exit_json(self, **kw): return kw
        basic.AnsibleModule = _StubModule
        sys.modules["ansible.module_utils.basic"] = basic
        sys.modules["ansible.module_utils"].basic = basic


_install_ansible_shim()


# ---------------------------------------------------------------------------
# Unit-test isolation: stub resolve_db_admin_route.
#
# Every write/smuggler/tasks/revisions/diagnostic kind starts by calling
# resolve_db_admin_route(target, db, ...) to route past a sharded
# orchestrator.  In a unit test that helper would hit the real network
# (load a cert, open an HTTPS socket).  Tests monkeypatch the kind's own
# `request` but not the one called from inside resolve_db_admin_route, so
# the helper dies on a fake cert path before the kind's own logic runs.
#
# This autouse fixture replaces resolve_db_admin_route with a pure
# passthrough across all modules that import it.  Integration tests opt
# out via marker (they need real routing against an embedded RavenDB).
# ---------------------------------------------------------------------------

_KINDS_WITH_ROUTE = (
    "ravendb_writes", "ravendb_smuggler", "ravendb_tasks",
    "ravendb_revisions", "ravendb_diagnostic",
)


def _passthrough_route(target, db, domain, client_cert, ca_cert):
    return target


@pytest.fixture(autouse=True)
def _stub_resolve_db_admin_route_for_unit(request, monkeypatch):
    """Auto-applied in unit tests; skipped in integration tests (real routing
    against an embedded RavenDB), and skipped in tests that specifically
    exercise resolve_db_admin_route itself (opt out by adding the marker
    `needs_real_route` to the test or by naming the file `*resolve_db_admin*`
    or `*_sharded.py`)."""
    fspath = str(getattr(request.node, "fspath", "")).replace("\\", "/")
    if "/integration/" in fspath:
        return
    fname = fspath.rsplit("/", 1)[-1]
    if "resolve_db_admin" in fname or fname.endswith("_sharded.py"):
        return
    if request.node.get_closest_marker("needs_real_route"):
        return
    import importlib
    for mod_name in _KINDS_WITH_ROUTE:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        if hasattr(mod, "resolve_db_admin_route"):
            monkeypatch.setattr(mod, "resolve_db_admin_route", _passthrough_route)


_BANNER_SEEN = set()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logstart(nodeid, location):
    file_path = nodeid.split("::", 1)[0]
    if file_path in _BANNER_SEEN:
        return
    _BANNER_SEEN.add(file_path)

    # The nodeid is relative to the pytest invocation cwd.  Resolve against
    # both the repo root and tests/ — whichever exists wins.
    candidates = [
        REPO_ROOT / file_path,
        REPO_ROOT / "tests" / file_path,
        Path(file_path),
    ]
    abs_path = next((c for c in candidates if c.exists()), None)
    if abs_path is None:
        return

    try:
        source = abs_path.read_text(encoding="utf-8")
        docstring = ast.get_docstring(ast.parse(source))
    except (OSError, SyntaxError):
        return
    if not docstring:
        return

    # Write directly to the real stdout + flush so the banner survives any
    # pytest output capture that may still be active inside this hook.
    out = sys.__stdout__
    out.write("\n\n\n")
    out.write("#" * 100 + "\n")
    out.write("#" + " " * 98 + "#\n")
    out.write("#  " + f"FILE: {file_path}".ljust(95) + " #\n")
    out.write("#" + " " * 98 + "#\n")
    out.write("#" * 100 + "\n")
    out.write(docstring.rstrip() + "\n")
    out.write("#" * 100 + "\n\n")
    out.flush()
