"""The LLM-authored entity selector that picks a run's held-out test set.

The wizard asks the operator to DESCRIBE how the test entities should be
chosen ("the 2024 judgments of the Kigali court", "these shared ids: ...",
"every entity modified since March"). A free-text instance search cannot
express that — it has no property filters, no dates, no id lists — so the
description is turned into a small Python selector by the LLM: the same
``query_entities`` / ``query_entities_full`` helpers a generated script already
uses, in a read-only namespace, with ``result`` holding the chosen entities.

Pure: the prompt builders, the output model, and the normalization of whatever
the selector put in ``result``. The execution lives in
:mod:`uwazi_admin_agent.use_cases.validation_sampling`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

SELECTOR_SYSTEM_PROMPT = """\
You write ONE short Python snippet that SELECTS the entities of a Uwazi test
set. The snippet is executed as-is in a read-only sandbox: it may only READ
entities and must set the variable `result`.

ALREADY BOUND — never import them, never define them:
  query_entities(mode, language='en', limit=10000, search_term=None,
                 template_name=None, filters=None, published=None,
                 shared_ids=None)
      mode "by_text"     — fuzzy free-text search (`search_term`).
      mode "by_filter"   — exact filters on the template's FILTERABLE
                           (`use_as_filter`) properties. `filters` is a list of
                           dicts, AND-combined:
                             {"property_name": <name>, "values": [<labels>]}
                             {"property_name": <name>, "date_from": "YYYY-MM-DD",
                              "date_to": "YYYY-MM-DD"}
                           `values` are thesaurus LABELS (never UUIDs).
      mode "by_template" — every entity of one template (`template_name`).
      mode "by_ids"      — the entities you name (`shared_ids`).
      All four return an OBJECT with ATTRIBUTE access: `res.summary.shared_ids`
      is the list of shared ids, `res.examples` a few entity dicts. Never
      subscript the result.
  query_entities_full(mode, language='en', limit=10000, search_term=None,
                      template_name=None, filters=None, published=None)
      Same modes EXCEPT "by_ids"; returns a list[dict] of full entity dicts
      (`shared_id`, `title`, `metadata`, `creation_date`, `edit_date`).
  json, re, datetime, collections, itertools are bound like modules
  (`json.loads`, `datetime.datetime.now()`, `re.search`, ...).

OUTPUT — set `result` to EITHER a list of shared ids (strings) or a list of
entity dicts. Anything else is rejected:
    ids = query_entities(mode="by_template", template_name="Judgment", limit=200)
    result = ids.summary.shared_ids[:30]

RULES
  - READ only. There are no write helpers and no LLM helpers; do not try.
  - Deterministic: there is no `random`, and repeated runs must select the
    same entities.
  - Copy template and property names EXACTLY as the schema spells them
    (Uwazi template names are case-sensitive), and only filter on properties
    marked [filterable].
  - An entity's metadata holds one entry per property. A per-language text
    value is a list of {"value": ..., "language": ...} entries; a thesaurus
    value is a label or UUID; a date is an ISO string.
  - Never hardcode a shared id unless the operator supplied it.
  - Bound the result: at most the requested number of entities.
  - Keep it short: one or two calls, no functions, no classes, no imports.

INSTANCE SCHEMA (the real templates, properties and languages — never invent):
{instance_schema}
"""


class ValidationSelector(BaseModel):
    """The selector code the LLM wrote for the operator's description."""

    python_code: str = Field(
        min_length=1,
        description="The snippet: read-only discovery calls that set `result` to the selected shared ids.",
    )
    description: str | None = Field(default=None, description="One sentence: which entities it selects and why.")


def build_selector_request(
    explanation: str,
    *,
    template_names: list[str],
    property_name: str,
    sample_size: int,
    language: str,
) -> str:
    """The user prompt for the selector turn (pure)."""
    templates = ", ".join(name for name in template_names if name.strip()) or "the templates the task targets"
    return "\n".join(
        [
            "OPERATOR DESCRIPTION of how the test entities must be chosen:",
            (explanation or "").strip() or "(none given)",
            "",
            "CONSTRAINTS",
            f"- Target template(s): {templates}",
            f"- Measured property (the score reads it; entities must already carry a value): {property_name}",
            f"- Language row to read: {language}",
            f"- Select at most {max(1, int(sample_size))} entities",
            "",
            "Write the selector snippet and set `result`.",
        ]
    )


def selector_entity_dicts(value: Any) -> dict[str, dict[str, Any]]:
    """The entity dicts a selector returned, keyed by shared id (pure).

    Accepts a list of dicts (``query_entities_full`` / ``by_ids`` shapes), a
    single dict, or a list of ids (which yields nothing — the caller fetches
    them). Non-dict entries are ignored rather than trusted.
    """
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for item in value:
        if isinstance(item, dict):
            shared_id = str(item.get("shared_id") or "").strip()
            if shared_id:
                found.setdefault(shared_id, item)
    return found


def selector_shared_ids(value: Any) -> list[str]:
    """The shared ids a selector returned, in order and deduplicated (pure).

    Accepts ids, entity dicts (``shared_id``), a result object (``.summary``
    with ``shared_ids``) or a single value, so an LLM that returns
    ``result = res`` instead of ``result = res.summary.shared_ids`` still works.
    """
    summary = getattr(value, "summary", None)
    if summary is not None:
        value = getattr(summary, "shared_ids", None) or []
    if isinstance(value, str):
        value = [value]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    ids: list[str] = []
    for item in value:
        if isinstance(item, dict):
            candidate = str(item.get("shared_id") or "").strip()
        else:
            candidate = str(item or "").strip()
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


__all__ = [
    "SELECTOR_SYSTEM_PROMPT",
    "ValidationSelector",
    "build_selector_request",
    "selector_entity_dicts",
    "selector_shared_ids",
]
