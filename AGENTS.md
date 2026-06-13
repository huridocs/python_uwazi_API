# Agent Instructions

Guidelines for coding agents working in this repository.

## Testing Policy

When asked to create or modify tests, follow these rules strictly:

1. **No real-instance tests.** Do not write tests that require a running Uwazi instance, external services, network calls, or environment credentials such as `UWAZI_URL`, `UWAZI_USER`, or `UWAZI_PASSWORD`.
2. **No mocks or stubs.** Do not use `unittest.mock`, `MagicMock`, `AsyncMock`, `monkeypatch`, fake repositories/adapters, or any other stand-in objects. Do not monkey-patch module attributes.
3. **Isolated unit tests only.** Test pure functions, value objects, and deterministic transformations with real inputs and plain assertions. Tests must run offline and in isolation, with no external dependencies.
4. **Run only the test you create.** After creating a test, execute just that file:

   ```bash
   python -m pytest path/to/test_your_module.py -v
   ```

   Do not run the full suite unless explicitly requested.

## Examples of acceptable tests

- Domain model construction and validation.
- Pure helper functions (e.g., string sanitization, formatting, conversions).
- In-memory stores that do not perform I/O.
- Deterministic rendering or parsing logic with literal inputs.

## Examples of unacceptable tests

- Tests that start Docker, call HTTP endpoints, or connect to a database.
- Tests that use `AsyncMock`, `MagicMock`, `monkeypatch`, or hand-rolled fake ports/repositories.
- Tests that rely on environment variables, file-system fixtures outside the test, or any real network.

## Existing Tests

Some existing tests use mocks or require a real Uwazi instance. Those are legacy tests and are not the target of this policy. Do not add new tests in that style. If you modify legacy tests, migrate them toward isolated unit tests where feasible.

## Project Conventions

- Use `pytest` for all tests.
- Place new test files under `uwazi_agent/tests/` or `uwazi_api/tests/` next to the code being tested.
- Name test files `test_<module>.py` and test functions `test_<behavior>`.
- Keep tests fast, deterministic, and self-contained.
