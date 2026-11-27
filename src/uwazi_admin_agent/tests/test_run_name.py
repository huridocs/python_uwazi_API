"""Isolated unit tests for run-name validation (pure, offline).

Per AGENTS.md: literal inputs, plain assertions, no mocks. A run name becomes a
folder under an instance's ``runs/`` dir, so a separator or ``..`` would write
into another run's — or another instance's — tree.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.run_name import free_copy_name, validate_run_name


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


def test_copy_name_is_the_plain_suffix_when_free() -> None:
    assert free_copy_name("merge-2026", ["other"]) == "merge-2026 copy"


def test_copy_name_numbers_past_every_taken_copy() -> None:
    # A colliding imported task must NOT target the first copy's folder.
    taken = ["merge-2026", "merge-2026 copy"]
    assert free_copy_name("merge-2026", taken) == "merge-2026 copy 2"


def test_copy_name_skips_gaps_and_never_reuses_a_taken_name() -> None:
    taken = ["merge-2026 copy", "merge-2026 copy 2", "merge-2026 copy 3"]
    assert free_copy_name("merge-2026", taken) == "merge-2026 copy 4"
