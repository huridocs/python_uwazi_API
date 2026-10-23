"""Isolated tests for the live-E2E localhost guard (per AGENTS.md: pure, offline).

``is_local_e2e_target`` and ``require_local_e2e_target`` are deterministic
transforms of a URL with literal inputs, so no instance, network, or
environment is involved.
"""

from __future__ import annotations

import pytest

from uwazi_api.testing.e2e_guard import (
    DEFAULT_UWAZI_URL,
    LocalE2ETargetError,
    is_local_e2e_target,
    require_local_e2e_target,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        "https://LOCALHOST:8443",
        "localhost:3000",
        "http://user:secret@localhost:3000",
    ],
)
def test_loopback_targets_are_accepted(url: str) -> None:
    assert is_local_e2e_target(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://production.uwazi.io",
        "https://staging.example.org",
        "http://localhost.evil.example",
        "http://127.0.0.1.nip.io:3000",
        "http://10.0.0.5:3000",
        "",
    ],
)
def test_non_loopback_targets_are_refused(url: str) -> None:
    assert is_local_e2e_target(url) is False


def test_require_returns_the_local_target_verbatim() -> None:
    assert require_local_e2e_target("http://localhost:3000") == "http://localhost:3000"


def test_require_explains_the_refusal_and_the_target() -> None:
    with pytest.raises(LocalE2ETargetError) as excinfo:
        require_local_e2e_target("https://production.uwazi.io")

    message = str(excinfo.value)
    assert "E2E tests refused" in message
    assert "https://production.uwazi.io" in message
    assert "just test_e2e" in message


def test_default_target_is_local() -> None:
    assert is_local_e2e_target(DEFAULT_UWAZI_URL) is True
