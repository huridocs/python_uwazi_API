"""Fixtures shared by the admin agent's live E2E modules.

The fixture itself calls the localhost guard before it builds a client. The
modules also carry an autouse guard, so every test is refused early if the
configured target stops being loopback.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.tests.e2e_support import UWAZI_URL, AdminE2EWorkspace
from uwazi_api.testing.e2e_guard import require_local_e2e_target


@pytest.fixture
def workspace(request: pytest.FixtureRequest) -> AdminE2EWorkspace:
    require_local_e2e_target(UWAZI_URL)
    return AdminE2EWorkspace(request)
