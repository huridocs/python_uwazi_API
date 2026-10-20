"""Isolated unit tests for the instance-language prompt context (per AGENTS.md).

Pure rendering over literal language models: no deps, no I/O, no network. The
fetch itself is exercised only by the live wiring (settings_api), same as the
other prompt-context fetches.
"""

from uwazi_admin_agent.use_cases.instance_languages import (
    render_language,
    render_languages_section,
    system_prompt_with_languages,
)
from uwazi_api.domain.language import Language

LANGUAGES = [
    Language(key="en", label="English", default=True),
    Language(key="es", label="Spanish"),
]


def test_render_language_includes_key_label_and_default() -> None:
    assert render_language(Language(key="en", label="English", default=True)) == "- en (English) [default]"
    assert render_language(Language(key="es", label="Spanish")) == "- es (Spanish)"
    assert render_language(Language(key="ar")) == "- ar"


def test_languages_section_lists_only_the_configured_keys() -> None:
    section = render_languages_section(LANGUAGES)

    assert section.startswith("INSTANCE LANGUAGES")
    assert "- en (English) [default]" in section
    assert "- es (Spanish)" in section
    assert "- fr" not in section


def test_languages_section_distinguishes_unavailable_from_empty() -> None:
    assert "unavailable" in render_languages_section(None)
    assert "- (none configured)" in render_languages_section([])


def test_system_prompt_appends_the_configured_languages_and_forbids_candidate_lists() -> None:
    prompt = system_prompt_with_languages("BASE CONTRACT", LANGUAGES)

    assert prompt.startswith("BASE CONTRACT")
    assert "- en (English) [default]" in prompt
    assert "- es (Spanish)" in prompt
    assert "CANDIDATE_LANGS" in prompt
    assert "NEVER hardcode" in prompt


def test_system_prompt_without_languages_forbids_inventing_them() -> None:
    prompt = system_prompt_with_languages("BASE CONTRACT", None)

    assert "could not be read" in prompt
    assert "NEVER hardcode or invent" in prompt
