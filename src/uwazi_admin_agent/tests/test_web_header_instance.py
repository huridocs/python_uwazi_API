"""Isolated unit tests for the header's targeted-instance label (pure)."""

from __future__ import annotations

from uwazi_admin_agent.drivers.web.header import instance_target


def test_instance_target_shows_the_url_a_page_was_built_for() -> None:
    assert instance_target("http://localhost:3000") == "http://localhost:3000"


def test_instance_target_trims_the_configured_url() -> None:
    """``UWAZI_URL`` may carry stray whitespace; the header shows a clean URL."""
    assert instance_target("  https://example.uwazi.io \n") == "https://example.uwazi.io"


def test_instance_target_is_empty_when_the_page_serves_no_instance() -> None:
    """No instance means no label: an empty badge is never rendered."""
    assert instance_target("") == ""
    assert instance_target("   ") == ""
