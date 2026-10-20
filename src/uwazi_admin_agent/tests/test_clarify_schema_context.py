"""Unit tests for the clarification schema-context renderer (§ clarification).

Pure, offline, no mocks: rendering templates/thesaurus names into the text
block the clarification system prompt embeds are deterministic transformations
of literal models; the no-deps degeneration is a fixed fallback string. The
live fetch against a real instance is the web driver's responsibility (same
validation path as the other live-wired use cases).
"""

from uwazi_admin_agent.domain.prompt_understanding import PromptUnderstanding  # noqa: F401 — import sanity
from uwazi_admin_agent.use_cases.clarify_schema_context import (
    fetch_schema_context,
    render_schema,
    render_template,
)
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_api.domain.language import Language


def _template(name: str, *props: AgentProperty) -> AgentTemplate:
    return AgentTemplate(name=name, properties=list(props))


def _prop(name: str, type_: str = "text", thesaurus_name: str | None = None, filterable: bool = False) -> AgentProperty:
    return AgentProperty(name=name, type=type_, thesaurus_name=thesaurus_name, use_as_filter=filterable)


def test_render_template_lists_properties_with_type_and_thesaurus() -> None:
    line = render_template(
        _template(
            "Judgment",
            _prop("summary", type_="text"),
            _prop("document_type", type_="multiselect", thesaurus_name="Document types", filterable=True),
        )
    )
    assert "Judgment" in line
    assert "summary (text)" in line
    assert "document_type (multiselect; thesaurus: Document types) [filterable]" in line


def test_render_template_without_properties_shows_placeholder() -> None:
    assert render_template(_template("Empty")) == "- Empty: (no custom properties)"


def test_render_schema_lists_templates_and_thesauri() -> None:
    block = render_schema([_template("A", _prop("x"))], ["Types"])
    assert "INSTANCE TEMPLATES" in block
    assert "- A: x (text)" in block
    assert "THESAURI NAMES" in block
    assert "- Types" in block


def test_render_schema_lists_configured_languages() -> None:
    block = render_schema(
        [],
        [],
        [Language(key="en", label="English", default=True), Language(key="es", label="Spanish")],
    )
    assert "INSTANCE LANGUAGES" in block
    assert "- en (English) [default]" in block
    assert "- es (Spanish)" in block
    assert "unavailable" not in block


def test_render_schema_language_section_distinguishes_unavailable_from_empty() -> None:
    unavailable = render_schema([], [], None)
    assert "INSTANCE LANGUAGES" in unavailable
    assert "(unavailable: the instance's language list could not be read)" in unavailable

    empty = render_schema([], [], [])
    assert "- (none configured)" in empty
    assert "unavailable" not in empty


def test_render_schema_empty_sections_renders_none_placeholders() -> None:
    block = render_schema([], [])
    assert "- (none)" in block


def test_fetch_schema_context_degrades_when_no_deps_wired() -> None:
    import asyncio

    text = asyncio.run(fetch_schema_context(None))
    assert "instance schema unavailable" in text
    assert "no template/thesaurus API wired" in text
