import json
import os
import shutil
import socket
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

# tests/conftest.py installs the ansible.module_utils shim + sys.path.
# Make sure that ran before we import any diagnostic-side modules.
import conftest  # noqa: F401  (the parent conftest)

# Add this directory so `from raven_lab import ...` works in integration tests.
sys.path.insert(0, str(Path(__file__).parent))

from ravendb_embedded import EmbeddedServer, ServerOptions


# Each "node" gets a chaos-style name (1a, 1b, 1c) so the diagnostic module's
# log lines match what scenarios print.  TARGET_URL_OVERRIDES maps that name
# to the embedded server's actual URL.
_NODE_LETTERS = "abcdefghi"


def _free_port():
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for(predicate, timeout_secs=20, poll_secs=0.5, what="condition"):
    deadline = time.monotonic() + timeout_secs
    last_err = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception as e:
            last_err = e
        time.sleep(poll_secs)
    raise TimeoutError("timed out waiting for %s (last error: %r)" % (what, last_err))


def _http(method, url, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _activate_license(url, license_obj):
    status, body = _http("POST", url + "/admin/license/activate", body=license_obj)
    if status not in (200, 201, 204):
        raise RuntimeError("license activation failed on %s: HTTP %d %s"
                           % (url, status, body[:300]))


def _start_node(http_port, tcp_port):
    # Fresh tempdirs per node per test -- no collision risk from stale runs.
    data_dir = tempfile.mkdtemp(prefix="raven-test-data-")
    logs_dir = tempfile.mkdtemp(prefix="raven-test-logs-")
    s = EmbeddedServer()
    opts = ServerOptions()
    opts.data_directory = data_dir
    opts.logs_path = logs_dir
    opts.server_url = "http://127.0.0.1:%d" % http_port
    opts.command_line_args = ["--ServerUrl.Tcp=tcp://127.0.0.1:%d" % tcp_port]
    s.start_server(opts)
    # Stash the dirs on the server object so the fixture can clean them up.
    s._test_dirs = (data_dir, logs_dir)
    return s


def _form_cluster(urls):
    """Add each non-leader URL to node-0's cluster as a Member."""
    leader = urls[0]
    for other in urls[1:]:
        status, body = _http("PUT", "%s/admin/cluster/node?url=%s" % (leader, other))
        if status not in (200, 201):
            raise RuntimeError(
                "cluster join failed (leader=%s other=%s): HTTP %d %s"
                % (leader, other, status, body[:300]))
    # Wait for every URL to appear as a Member in the leader's topology.
    def all_members():
        status, body = _http("GET", "%s/cluster/topology" % leader, timeout=5)
        if status != 200:
            return False
        members = json.loads(body).get("Topology", {}).get("Members") or {}
        return len(members) == len(urls)
    _wait_for(all_members, timeout_secs=30, what="all %d nodes to appear as Members" % len(urls))


def _create_database(leader_url, db, replication_factor):
    body = {
        "DatabaseName": db,
        "Settings": {},
        "Disabled": False,
    }
    status, resp = _http(
        "PUT",
        "%s/admin/databases?name=%s&replicationFactor=%d" % (leader_url, db, replication_factor),
        body=body,
    )
    if status not in (200, 201):
        raise RuntimeError("create_database failed: HTTP %d %s" % (status, resp[:300]))


def _wait_for_db_on_all(urls, db):
    def all_have_db():
        for u in urls:
            status, _ = _http("GET", "%s/databases/%s/stats" % (u, db), timeout=5)
            if status != 200:
                return False
        return True
    _wait_for(all_have_db, timeout_secs=30, what="db '%s' to appear on every node" % db)


@pytest.fixture
def ravendb_cluster():
    """Return a factory: cluster_info = ravendb_cluster(n_nodes=2, db="db1").

    cluster_info is a dict:
        {
          "nodes": ["1a", "1b"],      # chaos-style names, usable as `target`
          "urls":  ["http://127.0.0.1:..."]
          "db":    "db1",
        }

    TARGET_URL_OVERRIDES is wired so request("GET", "1a", ...) hits the right
    embedded server.  Cleaned up at end of test (servers killed, overrides cleared).
    """
    started = []
    saved_overrides = None

    def _build(n_nodes=1, db="db1", replication_factor=None, cluster=True):
        """Bring up N embedded RavenDB servers.

        cluster=True (default): form them into a real cluster + create the db
            with replication_factor=N (or override).  Requires a Developer
            license -- set RAVEN_TEST_LICENSE=/path/to/license.json.
        cluster=False: leave them as N independent servers, create db on each
            one separately (each gets its OWN dbid, so CVs naturally differ
            across nodes -- great for testing the FAIL paths of CV kinds).
        """
        nonlocal saved_overrides
        if replication_factor is None:
            replication_factor = n_nodes if cluster else 1

        # Activate a license whenever RAVEN_TEST_LICENSE is set -- needed for
        # both multi-node cluster formation AND for single-node features like
        # external_replication.  Multi-node clustered tests require the
        # license; tests using cluster=False that need licensed features will
        # also benefit when it's set.
        license_path = os.environ.get("RAVEN_TEST_LICENSE")
        if cluster and n_nodes > 1 and not license_path:
            pytest.skip(
                "Multi-node clustered tests need a Developer license.  "
                "Set RAVEN_TEST_LICENSE=/path/to/license.json to enable.")
        license_obj = None
        if license_path:
            with open(license_path) as f:
                license_obj = json.load(f)

        nodes, urls = [], []
        for i in range(n_nodes):
            http_p, tcp_p = _free_port(), _free_port()
            srv = _start_node(http_p, tcp_p)
            started.append(srv)
            url = srv.get_server_uri()
            urls.append(url)
            nodes.append("1" + _NODE_LETTERS[i])    # 1a, 1b, 1c, ...
            if license_obj is not None:
                _activate_license(url, license_obj)

        if cluster:
            if n_nodes > 1:
                _form_cluster(urls)
            _create_database(urls[0], db, replication_factor)
            _wait_for_db_on_all(urls, db)
        else:
            # Independent servers: create the db on each separately.
            for url in urls:
                _create_database(url, db, 1)

        # Wire up the override table so request() can reach these nodes.
        import ravendb_client
        saved_overrides = dict(ravendb_client.TARGET_URL_OVERRIDES)
        ravendb_client.TARGET_URL_OVERRIDES.clear()
        for n, u in zip(nodes, urls):
            ravendb_client.TARGET_URL_OVERRIDES[n] = u

        return {"nodes": nodes, "urls": urls, "db": db}

    yield _build

    # ---- teardown ----
    if saved_overrides is not None:
        import ravendb_client
        ravendb_client.TARGET_URL_OVERRIDES.clear()
        ravendb_client.TARGET_URL_OVERRIDES.update(saved_overrides)
    for srv in started:
        try:
            srv.close()
        except Exception:
            pass
        for d in getattr(srv, "_test_dirs", ()):
            shutil.rmtree(d, ignore_errors=True)
