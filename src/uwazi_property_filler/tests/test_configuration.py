from pathlib import Path

import pytest

from uwazi_property_filler.configuration import (
    PACKAGED_SUGGESTIONS_DIR,
    parse_instance_urls,
    select_instance_url,
    suggestions_dirs,
)


def test_parse_instance_urls_splits_and_strips() -> None:
    assert parse_instance_urls("http://a, https://b ,") == ["http://a", "https://b"]


def test_parse_instance_urls_deduplicates_keeping_the_first_occurrence() -> None:
    assert parse_instance_urls("http://a,http://a,http://b") == ["http://a", "http://b"]


def test_parse_instance_urls_empty_and_none() -> None:
    assert parse_instance_urls("") == []
    assert parse_instance_urls(None) == []


def test_select_instance_url_uses_the_single_configured_url() -> None:
    assert select_instance_url("https://only.example") == "https://only.example"


def test_select_instance_url_uses_first_of_a_list() -> None:
    assert select_instance_url("https://a.example,https://b.example") == "https://a.example"


def test_select_instance_url_override_wins_over_the_list() -> None:
    assert select_instance_url("https://a.example,https://b.example", "https://b.example") == "https://b.example"


def test_select_instance_url_resolves_a_list_override_to_its_first_entry() -> None:
    """``PROPERTY_FILLER_UWAZI_URL`` accepts the same list shape as ``UWAZI_URL``."""
    assert select_instance_url("https://a.example", "https://b.example,https://c.example") == "https://b.example"


def test_select_instance_url_requires_a_url() -> None:
    with pytest.raises(RuntimeError, match="UWAZI_URL is not set"):
        select_instance_url(None)


def test_suggestions_dirs_always_include_the_packaged_directory() -> None:
    dirs = suggestions_dirs("")

    assert dirs[0] == PACKAGED_SUGGESTIONS_DIR
    assert PACKAGED_SUGGESTIONS_DIR.is_dir()


def test_suggestions_dirs_split_the_configured_list() -> None:
    dirs = suggestions_dirs("/a, /b ,")

    assert dirs[1:] == [Path("/a"), Path("/b")]


def test_suggestions_dirs_fall_back_to_the_runtime_dir_when_it_exists(tmp_path: Path) -> None:
    runtime = tmp_path / "suggestions"
    runtime.mkdir()

    assert suggestions_dirs("", default_runtime=runtime) == [PACKAGED_SUGGESTIONS_DIR, runtime]


def test_suggestions_dirs_omit_the_runtime_dir_when_it_does_not_exist(tmp_path: Path) -> None:
    assert suggestions_dirs("", default_runtime=tmp_path / "missing") == [PACKAGED_SUGGESTIONS_DIR]
