"""Repo-wide pytest configuration and the hard safety gate for live E2E tests.

Live end-to-end tests are marked ``e2e`` and are excluded from the default run
(``addopts`` in ``pyproject.toml``). They run explicitly with ``just test_e2e``,
against a LOCAL Uwazi instance only.

This file is the safety net: if any ``e2e`` test is selected and ``UWAZI_URL``
is not a loopback address, collection is refused with a clear message before a
single client is built. A new E2E module that forgets both the marker and the
in-test guard is still refused, because ``*_e2e.py`` files are auto-marked here.
"""

from __future__ import annotations

import pytest
from dotenv import load_dotenv

from uwazi_api.testing.e2e_guard import LocalE2ETargetError, require_local_e2e_target

# Match the E2E modules: they load ``.env`` themselves at import time, but the
# gate may run before those imports, so load it here too.
load_dotenv()


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark every ``*_e2e.py`` module as e2e, even if it omits the marker."""
    del config  # unused; the hook signature is fixed by pytest
    for item in items:
        if item.path.name.endswith("_e2e.py"):
            item.add_marker(pytest.mark.e2e)


def pytest_collection_finish(session: pytest.Session) -> None:
    """Refuse the whole run up-front when a selected e2e test targets a non-local host."""
    if any(item.get_closest_marker("e2e") is not None for item in session.items):
        try:
            require_local_e2e_target()
        except LocalE2ETargetError as exc:
            raise pytest.UsageError(str(exc)) from exc


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Per-test backstop for runners that bypass the collection gate."""
    if item.get_closest_marker("e2e") is not None:
        try:
            require_local_e2e_target()
        except LocalE2ETargetError as exc:
            pytest.fail(str(exc), pytrace=False)
