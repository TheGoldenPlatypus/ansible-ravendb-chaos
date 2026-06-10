# tests/

Test suite for `library/ravendb_diagnostic.py` (and friends).

## Layout

```
tests/
  conftest.py            # shared pytest fixtures (mock_request, params builders, ...)
  unit/                  # mocked-HTTP, fast (<5s total). Edge cases, parsing, false-alarm hunts.
  integration/           # RavenTestDriver, slow. End-to-end smoke against real RavenDB.
```

## Running

```bash
# From repo root:
PYTHONPATH=. pytest tests/unit -v                  # fast loop
PYTHONPATH=. pytest tests/integration -v           # slow; needs RavenDB available
PYTHONPATH=. pytest tests -v                       # everything
```

## Why two flavors

- **Unit tests** mock the `request()` HTTP helper so we control exactly what each "node"
  returns. This is what lets us reproduce false alarms (malformed CV, partial response,
  500 errors) deterministically without a real server. Fast feedback for understanding
  what each diagnostic kind actually does.

- **Integration tests** use the existing `RavenTestDriver` (same pattern as the upstream
  ravendb collection's `tests/unit/test_database.py`). These prove the unit-mocked
  assumptions match what real RavenDB actually returns. Catches drift if Raven's API
  shape changes.

## Conventions

- One test file per diagnostic kind (or per helper module).
- Test names spell out the property under test: `test_returns_pass_when_all_nodes_agree`,
  not `test_happy_path`. The test name + assertion is the documentation.
- When a test uncovers a real bug, mark it `pytest.xfail(reason="TODO Phase 2: ...")`
  with a one-line description. The xfail list becomes the Phase-2 backlog.
- Keep each unit test under 30 lines. Heavy setup goes in `conftest.py` as a fixture.
