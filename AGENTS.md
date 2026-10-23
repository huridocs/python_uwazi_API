# Agent Instructions

Guidelines for coding agents working in this repository.

## Testing Policy

The repo has **two test tiers** with different rules. Choose the tier before you
write a test.

### Tier 1 — Unit tests (the default suite, `just test`)

These must run **offline, in isolation, and deterministically**. This is what
`just test` runs, and it must never require a live service.

1. **No real-instance tests.** Do not write unit tests that require a running
   Uwazi instance, external services, network calls, or environment credentials
   such as `UWAZI_URL`, `UWAZI_USER`, or `UWAZI_PASSWORD`.
2. **No mocks or stubs.** Do not use `unittest.mock`, `MagicMock`, `AsyncMock`,
   `monkeypatch`, fake repositories/adapters, or any other stand-in objects. Do
   not monkey-patch module attributes.
3. **Isolated unit tests only.** Test pure functions, value objects, and
   deterministic transformations with real inputs and plain assertions.
4. **Run only the test you create.** After creating a test, execute just that
   file:

   ```bash
   python -m pytest path/to/test_your_module.py -v
   ```

   Do not run the full suite unless explicitly requested.

### Tier 2 — End-to-end tests (`just test_e2e`)

E2E tests are the **only** tests allowed to touch a live Uwazi instance. They
validate the real wiring against a real server and are **opt-in** — never part
of the default suite.

#### MANDATORY: E2E tests may only run against localhost

Live E2E tests create, modify, and delete real data. A single wrong `UWAZI_URL`
must never be able to write to production. Therefore **every E2E test file MUST
have the localhost guard as its first action**:

1. Name the file `test_<module>_e2e.py`.
2. Mark the module with the `e2e` marker:
   ```python
   pytestmark = pytest.mark.e2e
   ```
3. Import and call the shared guard **before** any client/adapter is built:
   ```python
   import os

   import pytest
   from dotenv import load_dotenv

   from uwazi_api.client import UwaziClient
   from uwazi_api.testing.e2e_guard import require_local_e2e_target

   load_dotenv()

   UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")

   pytestmark = pytest.mark.e2e


   class TestSomethingE2E:
       @classmethod
       def setup_class(cls):
           # MUST be the first statement: refuse anything that is not loopback.
           require_local_e2e_target(UWAZI_URL)
           cls.client = UwaziClient(user=..., password=..., url=UWAZI_URL)
   ```

   For a non-class-based test, call `require_local_e2e_target(UWAZI_URL)` as the
   first statement of the test function (or of an autouse fixture that runs
   before it).

   Only `localhost`, `127.0.0.1`, and `::1` are accepted. Any other host is
   refused with a clear error message and **nothing is sent to the target**.

4. **Never bypass, weaken, or re-implement this check.** Do not replace it with
   a hostname heuristic, a `skip`, a warning, or an environment-variable
   escape hatch.

The root `conftest.py` provides two safety nets on top of the in-test call:
`*_e2e.py` modules are auto-marked `e2e`, and a run that selects any e2e test
is refused during collection unless `UWAZI_URL` is loopback. A new E2E file
that forgets the marker and the guard is still refused.

#### Running E2E tests

- `just test_e2e` runs the live suite (`pytest -m e2e`). It requires a local
  Uwazi instance reachable via `UWAZI_URL` (or `.env`). The default is
  `http://localhost:3000`.
- `just test` runs only the offline suite (`pytest -m "not e2e"`). Do not add a
  live dependency to it.
- To run a single E2E file:
  ```bash
  uv run python -m pytest -m e2e src/uwazi_api/tests/test_my_thing_e2e.py -v
  ```
- The admin agent's live exercise is a separate CLI driver, not a pytest file:
  ```bash
  uv run uwazi-admin-agent text-property-e2e
  ```
  It applies the same localhost guard (`require_local_e2e_target`) and refuses
  any non-loopback `UWAZI_URL` with exit code 2 before contacting the instance.
- Never point E2E tests at production, staging, a shared demo, or any instance
  you do not own. "Localhost" means a disposable instance you control.

#### E2E test rules

- **Idempotent by construction.** Snapshot the live state before writing, restore
  it in a `finally` block, and remove anything the test created. A second run
  must start from the same state as the first.
- **Self-contained fixtures.** Create the templates/thesauri/entities the test
  needs instead of assuming they already exist.
- **Real adapters only.** E2E tests must not use mocks or stubs either; the
  point is to exercise the real client, HTTP, and serialization paths.
- **Deterministic cleanup.** Use unique names/markers (e.g. a timestamp) so a
  failed previous run cannot collide with the next one.
- **Do not add live tests to `uwazi_admin_agent`'s unit suite.** Admin-agent E2E
  coverage lives in the guarded `*_e2e.py` modules under
  `src/uwazi_admin_agent/tests/` (task creation, execute/revert, update-table,
  and per-language rows) and in the `text-property-e2e` driver
  (`drivers/step_text_property_e2e.py`). The pytest modules use the real
  service layer and deterministic seeded scripts, not the web UI or an LLM.
  Pure helpers belong in the offline suite.

### Examples of acceptable unit tests

- Domain model construction and validation.
- Pure helper functions (e.g., string sanitization, formatting, conversions).
- In-memory stores that do not perform I/O.
- Deterministic rendering or parsing logic with literal inputs.

### Examples of unacceptable unit tests

- Tests that start Docker, call HTTP endpoints, or connect to a database.
- Tests that use `AsyncMock`, `MagicMock`, `monkeypatch`, or hand-rolled fake
  ports/repositories.
- Tests that rely on environment variables, file-system fixtures outside the
  test, or any real network.

These are acceptable **only** as E2E tests under the localhost guard above.

## Existing Tests

Some existing tests use mocks or require a real Uwazi instance. Those are legacy
tests and are not the target of the unit-test policy. The live ones have been
moved behind the `e2e` marker and the localhost guard; keep them there. If you
modify legacy tests, migrate them toward isolated unit tests where feasible.

## Project Conventions

- Use `pytest` for all tests.
- Place new test files under `uwazi_agent/tests/`, `uwazi_api/tests/`, or the
  package they exercise. E2E files must end in `_e2e.py` and carry
  `pytestmark = pytest.mark.e2e`.
- Name test files `test_<module>.py` (unit) or `test_<module>_e2e.py` (E2E) and
  test functions `test_<behavior>`.
- Keep unit tests fast, deterministic, and self-contained.
- Keep E2E tests localhost-only, idempotent, and explicitly run with
  `just test_e2e`.
