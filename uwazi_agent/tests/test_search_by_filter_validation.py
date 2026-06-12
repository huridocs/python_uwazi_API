"""Unit tests for ``UwaziApiAdapter.search_entities_by_filter`` pre-validation.

The pre-validation runs before any HTTP call, so we exercise it with a
fake ``template_repo`` and ``search_repo`` — no live Uwazi instance needed.
"""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_agent.use_cases.tools.search_entities_by_filter import search_entities_by_filter
from uwazi_api.domain.exceptions import PropertyNotFilterableError
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _make_adapter(template: Template) -> UwaziApiAdapter:
    adapter = UwaziApiAdapter.__new__(UwaziApiAdapter)
    adapter._template_repo = MagicMock()
    adapter._template_repo.get_by_name.return_value = template
    adapter._search_repo = MagicMock()
    adapter._search_repo.search_by_filter.return_value = []
    adapter._entity_mapper = MagicMock()
    adapter._thesauri_repo = MagicMock()
    adapter._entity_repo = MagicMock()
    adapter._pages_repo = MagicMock()
    adapter._relationship_repo = MagicMock()
    adapter._settings_repo = MagicMock()
    adapter._menu_links_repo = MagicMock()
    adapter._stats_repo = MagicMock()
    adapter._template_mapper = MagicMock()
    adapter._page_mapper = MagicMock()
    return adapter


def _films_template() -> Template:
    return Template(
        name="Films",
        properties=[
            PropertySchema(name="country", label="Country", type=PropertyType.SELECT, filter=True),
            PropertySchema(name="year", label="Year", type=PropertyType.NUMERIC, filter=True),
            PropertySchema(name="description", label="Description", type=PropertyType.TEXT, filter=False),
        ],
    )


class TestSearchByFilterPreValidation:
    def test_filterable_property_passes_through(self):
        adapter = _make_adapter(_films_template())
        filters = [AgentSearchFilter(property_name="country", values=["Japan"])]
        _run(adapter.search_entities_by_filter("Films", filters, "en", limit=10))
        assert adapter._search_repo.search_by_filter.called

    def test_non_filterable_property_raises_property_not_filterable_error(self):
        adapter = _make_adapter(_films_template())
        filters = [AgentSearchFilter(property_name="description", values=["something"])]
        with pytest.raises(PropertyNotFilterableError) as exc_info:
            _run(adapter.search_entities_by_filter("Films", filters, "en", limit=10))
        assert exc_info.value.property_name == "description"
        assert exc_info.value.template_name == "Films"
        assert sorted(exc_info.value.filterable_properties) == ["country", "year"]
        assert "description" not in exc_info.value.filterable_properties
        assert not adapter._search_repo.search_by_filter.called

    def test_unknown_property_is_silently_passed_to_repo(self):
        """A property the template does not define is not our concern here —
        the underlying ``search_by_filter`` will surface a clear
        'Property X not found' error which the tool layer already formats."""
        adapter = _make_adapter(_films_template())
        filters = [AgentSearchFilter(property_name="nonexistent_prop", values=["x"])]
        _run(adapter.search_entities_by_filter("Films", filters, "en", limit=10))
        assert adapter._search_repo.search_by_filter.called

    def test_unknown_template_is_silently_passed_to_repo(self):
        """If the template is not in the cache, the adapter does not have the
        information needed to validate and must fall through to the
        underlying repo, which will raise a 'Template not found' error."""
        adapter = _make_adapter(_films_template())
        adapter._template_repo.get_by_name.return_value = None
        filters = [AgentSearchFilter(property_name="country", values=["Japan"])]
        _run(adapter.search_entities_by_filter("Unknown", filters, "en", limit=10))
        assert adapter._search_repo.search_by_filter.called

    def test_mixed_filterable_and_non_filterable_short_circuits(self):
        adapter = _make_adapter(_films_template())
        filters = [
            AgentSearchFilter(property_name="country", values=["Japan"]),
            AgentSearchFilter(property_name="description", values=["x"]),
        ]
        with pytest.raises(PropertyNotFilterableError) as exc_info:
            _run(adapter.search_entities_by_filter("Films", filters, "en", limit=10))
        assert exc_info.value.property_name == "description"
        assert not adapter._search_repo.search_by_filter.called

    def test_no_filters_skips_validation(self):
        adapter = _make_adapter(_films_template())
        _run(adapter.search_entities_by_filter("Films", [], "en", limit=10))
        assert adapter._search_repo.search_by_filter.called


def _make_run_context(deps: UwaziAgentToolsDependencies) -> Any:
    """Minimal stand-in for ``pydantic_ai.tools.RunContext`` — the only
    attribute the tool body reads is ``ctx.deps``."""

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.deps = deps
    return ctx


def _make_deps_with_entity_api(entity_api: Any) -> UwaziAgentToolsDependencies:
    return UwaziAgentToolsDependencies.model_construct(
        thesauri_api=MagicMock(),
        template_api=MagicMock(),
        template_mapper=MagicMock(),
        stats_api=MagicMock(),
        relationship_type_api=MagicMock(),
        entity_api=entity_api,
        page_api=MagicMock(),
        settings_api=MagicMock(),
        entity_store=MagicMock(),
        schema_store=MagicMock(),
        tool_cache=MagicMock(),
        tool_progress=[],
    )


class TestSearchByFilterToolErrorMessage:
    def test_property_not_filterable_returns_actionable_string(self):
        entity_api = AsyncMock()
        entity_api.search_entities_by_filter.side_effect = PropertyNotFilterableError(
            property_name="description",
            template_name="Films",
            filterable_properties=["country", "year"],
        )
        deps = _make_deps_with_entity_api(entity_api)
        ctx = _make_run_context(deps)
        filters = [AgentSearchFilter(property_name="description", values=["x"])]

        result = _run(search_entities_by_filter(ctx, "Films", filters, "en", limit=10))

        assert isinstance(result, str)
        assert result.startswith("Error")
        assert "description" in result
        assert "Films" in result
        assert "use_as_filter" in result
        assert "country" in result
        assert "year" in result
        assert "get_templates_by_names" in result
