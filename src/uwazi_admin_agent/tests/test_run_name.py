"""Isolated unit tests for run-name validation (pure, offline).

Per AGENTS.md: literal inputs, plain assertions, no mocks. A run name becomes a
folder under an instance's ``runs/`` dir, so a separator or ``..`` would write
into another run's — or another instance's — tree.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.run_name import validate_run_name


@pytest.mark.parametrize("name", ["merge-2026", "a", "UPPER case", "dots.in.name", "with space", "-lead"])
def test_a_plain_name_is_accepted_unchanged(name: str) -> None:
    assert validate_run_name(name) == name


def test_surrounding_whitespace_is_stripped() -> None:
    assert validate_run_name("  run-1  ") == "run-1"


@pytest.mark.parametrize(
    "name",
    [
        "../../other-instance/runs/x",
        "../x",
        "a/b",
        "a\\b",
        "/etc/passwd",
        ".",
        "..",
        "",
        "   ",
        "line\nbreak",
        "nul\x00byte",
    ],
)
def test_an_unsafe_name_is_refused(name: str) -> None:
    with pytest.raises(ValueError):
        validate_run_name(name)
