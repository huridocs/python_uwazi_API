"""Isolated tests for the LLM-authored validation selector (per AGENTS.md).

Real objects only: a small in-memory ``EntityApiPort`` and literal selector
snippets. No mocks, no network, no LLM (the turn that WRITES the snippet is not
part of this tier).
"""

from __future__ import annotations

import asyncio
from typing import Any

from uwazi_admin_agent.domain.guided_task import (
    GOAL_CREATE,
    WizardProperty,
    WizardSchema,
    WizardTemplate,
    is_writable_property,
    property_choices,
    template_property_choices,
)
from uwazi_admin_agent.domain.validation import VALIDATION_CUSTOM, ValidationConfig
from uwazi_admin_agent.domain.validation_selector import (
    SELECTOR_SYSTEM_PROMPT,
    build_selector_request,
    selector_entity_dicts,
    selector_shared_ids,
)
from uwazi_admin_agent.use_cases.validation_sampling import resolve_validation_set
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


def _result(entities: list[AgentEntity]) -> AgentEntitySearchResult:
    result = AgentEntitySearchResult(
        summary=AgentEntitySummary(
            count=len(entities),
            by_template={},
            sample_titles=[entity.title or "" for entity in entities[:3]],
            shared_ids=[entity.shared_id for entity in entities[:3]],
        ),
        examples=entities[:3],
    )
    result._all_entities = entities
    return result


class _InMemoryEntityApi:
    """A real (tiny) ``EntityApiPort``: template listing, ids, and select filters."""

    def __init__(self, entities: list[AgentEntity]) -> None:
        self.entities = list(entities)

    async def get_entities_by_shared_ids(self, shared_ids, language, limit=10000):
        wanted = set(shared_ids[:limit])
        return [entity for entity in self.entities if entity.shared_id in wanted]

    async def search_entities_by_text(self, search_term, template_name, language, limit):
        return _result([entity for entity in self.entities if search_term in (entity.title or "")])

    async def get_entities_by_template(self, template_name, language, limit):
        return _result([entity for entity in self.entities if entity.template_name == template_name])

    async def search_entities_by_filter(self, template_name, filters, language, limit, published=None):
        selected = [e for e in self.entities if e.template_name == template_name]
        for condition in filters:
            if condition.values is not None:
                selected = [
                    entity
                    for entity in selected
                    if set(condition.values) & set(entity.metadata.get(condition.property_name) or [])
                ]
        return _result(selected)


def _entity(shared_id: str, *, court: str = "Kigali", ref: str = "2024/1") -> AgentEntity:
    return AgentEntity(
        shared_id=shared_id,
        title=f"Judgment {shared_id}",
        template_name="Judgment",
        metadata={"court": [court], "document_ref": ref},
    )


# --- selector result normalization (pure) ------------------------------------


def test_shared_ids_accept_ids_dicts_and_the_unreduced_result_object() -> None:
    assert selector_shared_ids(["a", "b", "a"]) == ["a", "b"]
    assert selector_shared_ids([{"shared_id": "a"}, {"shared_id": ""}, "b"]) == ["a", "b"]
    assert selector_shared_ids(_result([_entity("a"), _entity("b")])) == ["a", "b"]
    assert selector_shared_ids("a") == ["a"]
    assert selector_shared_ids(None) == []


def test_entity_dicts_keep_the_best_entity_per_id() -> None:
    found = selector_entity_dicts([{"shared_id": "a", "title": "A"}, {"shared_id": "a", "title": "B"}])

    assert found == {"a": {"shared_id": "a", "title": "A"}}


def test_the_selector_request_names_the_constraints_it_must_respect() -> None:
    request = build_selector_request(
        "only the Kigali judgments",
        template_names=["Judgment"],
        property_name="document_ref",
        sample_size=12,
        language="en",
    )

    assert "only the Kigali judgments" in request
    assert "Judgment" in request and "document_ref" in request and "12" in request
    # The system prompt embeds the instance schema at run time.
    assert "{instance_schema}" in SELECTOR_SYSTEM_PROMPT


# --- sampling with an LLM-authored selector ---------------------------------


def _config(code: str, **overrides: Any) -> ValidationConfig:
    values: dict[str, Any] = {
        "mode": VALIDATION_CUSTOM,
        "property_name": "document_ref",
        "property_type": "text",
        "template_names": ["Judgment"],
        "sample_size": 10,
        "selector_code": code,
        "selector_explanation": "only the Kigali judgments",
        "language": "en",
    }
    values.update(overrides)
    return ValidationConfig(**values)


def test_the_selector_chooses_the_entities_and_their_labels_are_frozen() -> None:
    api = _InMemoryEntityApi([_entity("a"), _entity("b", court="Mombasa"), _entity("c")])
    code = (
        "found = query_entities(mode='by_filter', template_name='Judgment',\n"
        "                       filters=[{'property_name': 'court', 'values': ['Kigali']}])\n"
        "result = found.summary.shared_ids\n"
    )

    validation = asyncio.run(resolve_validation_set(api, "run-1", _config(code)))

    assert validation is not None
    assert validation.shared_ids == ["a", "c"]
    assert validation.labels == {"a|en": "2024/1", "c|en": "2024/1"}
    assert validation.mode == VALIDATION_CUSTOM
    assert validation.selector_explanation == "only the Kigali judgments"


def test_ids_the_selector_returns_without_dicts_are_fetched_and_scored() -> None:
    api = _InMemoryEntityApi([_entity("a"), _entity("b")])

    validation = asyncio.run(resolve_validation_set(api, "run-1", _config("result = ['b', 'missing']")))

    assert validation is not None
    assert validation.shared_ids == ["b"]


def test_entities_without_the_measured_value_are_not_part_of_the_set() -> None:
    api = _InMemoryEntityApi([_entity("a", ref=""), _entity("b", ref="2024/9")])

    validation = asyncio.run(resolve_validation_set(api, "run-1", _config("result = ['a', 'b']")))

    assert validation is not None
    assert validation.shared_ids == ["b"]


def test_a_selector_that_writes_is_refused_by_the_sandbox() -> None:
    api = _InMemoryEntityApi([_entity("a")])

    try:
        asyncio.run(resolve_validation_set(api, "run-1", _config("update_entities([])\nresult = []")))
    except ValueError as exc:
        assert "NameError" in str(exc) or "not allowed" in str(exc)
    else:  # pragma: no cover - the sandbox must refuse it
        raise AssertionError("a selector reached a write helper")


# --- writable targets -------------------------------------------------------


def _schema() -> WizardSchema:
    template = WizardTemplate(
        name="Judgment",
        properties=(
            WizardProperty(name="title", label="Title", type="text"),
            WizardProperty(name="judge", label="Judge", type="text"),
            WizardProperty(name="creationDate", label="Date added", type="date"),
            WizardProperty(name="editDate", label="Date modified", type="date"),
        ),
    )
    return WizardSchema(templates=(template,))


def test_uwazi_managed_dates_are_never_a_write_target_but_are_an_input() -> None:
    assert not is_writable_property("creationDate")
    assert not is_writable_property("editDate")

    targets = [name for name, _display in property_choices(_schema(), ["Judgment"])]
    assert targets == ["judge", "title"]

    inputs = [name for name, _display in template_property_choices(_schema(), "Judgment")]
    assert "creationDate" in inputs and "editDate" in inputs

    create_targets = [name for name, _display in template_property_choices(_schema(), "Judgment", writable_only=True)]
    assert create_targets == ["judge", "title"]


def test_a_create_flow_target_choice_never_offers_the_managed_dates() -> None:
    schema = _schema()
    assert GOAL_CREATE == "create"
    assert "creationDate" not in dict(template_property_choices(schema, "Judgment", writable_only=True))


# --- the selector's real return shapes and the named-id fallback -------------


def test_an_error_string_is_never_read_as_a_shared_id() -> None:
    """``query_entities(mode="by_ids")`` returns a LIST; using it as an object errors."""
    assert selector_shared_ids("Error: 'by_ids' mode requires `shared_ids` (a non-empty list).") == []


def test_by_ids_selector_reads_the_entity_dicts_it_gets() -> None:
    """The bound helper returns entity dicts, so the snippet must read ``d["shared_id"]``."""
    api = _InMemoryEntityApi([_entity("a"), _entity("b")])
    code = "result = [d['shared_id'] for d in query_entities(mode='by_ids', shared_ids=['a'])]\n"

    validation = asyncio.run(resolve_validation_set(api, "run-1", _config(code)))

    assert validation is not None
    assert validation.shared_ids == ["a"]


def test_a_selector_that_selects_nothing_falls_back_to_the_ids_the_operator_named() -> None:
    """The reported failure: "these shared ids: ..." produced an EMPTY set."""
    first, second, other = "6g14bm8ub1a", "9fp3cucotxf", "zzzzzzzzzzz"
    api = _InMemoryEntityApi([_entity(first), _entity(second), _entity(other)])
    config = _config("result = []", selector_explanation=f"these shared ids: {first}, {second}")

    validation = asyncio.run(resolve_validation_set(api, "run-1", config))

    assert validation is not None
    assert set(validation.shared_ids) == {first, second}


def test_a_named_sample_is_used_verbatim_without_running_the_selector() -> None:
    """A copied task's frozen sample must not be re-selected by the model."""
    api = _InMemoryEntityApi([_entity("a"), _entity("b"), _entity("c")])
    config = _config("raise AssertionError('the selector must not run')").model_copy(update={"shared_ids": ["c"]})

    validation = asyncio.run(resolve_validation_set(api, "run-1", config))

    assert validation is not None
    assert validation.shared_ids == ["c"]


def test_shared_ids_in_text_finds_the_identifiers_an_operator_typed() -> None:
    from uwazi_admin_agent.domain.validation_selector import shared_ids_in_text

    found = shared_ids_in_text("use entities with shared id 6g14bm8ub1a, 9fp3cucotxf")
    assert "6g14bm8ub1a" in found and "9fp3cucotxf" in found
