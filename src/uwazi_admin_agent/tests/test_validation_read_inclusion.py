"""Isolated tests for prepending the frozen sample to a scoring-pass read (per AGENTS.md).

The scoring pass used to NARROW a target-template read to the sampled ids. A
relationship script resolves its values against a catalog, and that catalog can
live in the same template: the narrowed read came back empty, every held-out
entity recorded ``skipped``, and the validation table showed no suggested value.
The sample is prepended instead, so the script's own slice starts with the
scored entities while the rest of the page stays readable.

Real objects: plain entity dicts and the real ``AgentEntitySummary``.
"""

from __future__ import annotations

from uwazi_admin_agent.use_cases.script_exec_namespace.constants import NamedNamespace
from uwazi_admin_agent.use_cases.script_exec_namespace.scope import include_held_entities
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary


def _entity(shared_id: str, title: str = "") -> dict[str, str]:
    return {"shared_id": shared_id, "title": title or shared_id}


def _ids(items: list[dict[str, str]]) -> list[str]:
    return [item["shared_id"] for item in items]


def test_the_sample_is_moved_first_and_the_rest_kept() -> None:
    """A script that slices (``dicts[:2]``) must start at the scored entity."""
    page = [_entity("a"), _entity("b"), _entity("held")]

    included = include_held_entities(page, [_entity("held")])

    assert _ids(included) == ["held", "a", "b"]


def test_a_sample_entity_already_in_the_page_is_not_duplicated() -> None:
    page = [_entity("held"), _entity("a")]

    included = include_held_entities(page, [_entity("held")])

    assert _ids(included) == ["held", "a"]


def test_a_summary_view_puts_the_sample_ids_first_and_keeps_its_count() -> None:
    """The count/by_template describe the WHOLE search, not the prepended sample."""
    summary = AgentEntitySummary(count=5000, shared_ids=["a", "held"], by_template={"Decision": 5000})
    view = NamedNamespace(summary=summary, examples=[_entity("a")])

    included = include_held_entities(view, [_entity("held")])

    assert included.summary.shared_ids == ["held", "a"]
    assert included.summary.count == 5000
    assert included.summary.by_template == {"Decision": 5000}
    assert _ids(included.examples) == ["held", "a"]


def test_an_error_string_and_an_empty_sample_pass_through() -> None:
    error = "Error: read budget of 10000 entities exhausted for this pass."
    assert include_held_entities(error, [_entity("held")]) == error

    page = [_entity("a")]
    assert include_held_entities(page, []) is page
