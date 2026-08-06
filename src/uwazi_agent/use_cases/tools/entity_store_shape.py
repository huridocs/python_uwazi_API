"""Helpers that describe the *shape* of the entity store.

The Python sub-agent iterates ``entities`` directly. To keep its single
``run_python_code`` call focused on the user's question (and to avoid
discovering the schema with a wasteful introspection pass), the
orchestrator pre-computes a compact **entity-store shape block** and
injects it at delegation time. This module owns that block.

The block covers four concerns, in order:

1. **Top-level keys** every entity always carries (with their types and
   the nullability of the value).
2. **Per-template breakdown** (count, plus the metadata keys seen).
3. **Per metadata property**, the type, a sample value drawn from the
   store, the fraction of entities that have a non-None value, and the
   template-level flags (``required``, ``use_as_filter``,
   ``show_in_card``). When the template is known the type comes from
   the schema; otherwise it is inferred from the first non-None value
   the store actually carries.
4. **Earliest and latest** by ``creation_date`` so time-based questions
   ("first/last Book", "oldest entity") can be answered without sorting
   the store at all.

Nullability is critical: the agent must learn that an entity may
**not have a key at all** in its metadata dict when the template
defines a property the entity never set. See
:func:`uwazi_agent.adapters.uwazi_api.entity_mapper.to_agent`, which
drops any metadata key that does not match a template property, and
never inserts a placeholder for missing properties. The shape block
distinguishes three cases:

* **never missing** — the key is present on every entity in the store
  (and non-None).
* **sometimes missing** — the key is on some entities, missing on
  others (the agent must guard with ``e['metadata'].get('key')``).
* **always missing** — the key is on no entity in the store (the
  template defines the property but the store has no value; reading
  the value as a real property will raise ``KeyError``).

When the orchestrator does not have the template cached (e.g. the
agent fetched entities via ``by_text`` and the template was not
separately loaded), the type for each metadata key is best-effort,
inferred from the first non-None value. The block notes this with
``(inferred)`` so the agent knows it is not authoritative.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.use_cases.tools.entity_store import EntityStore

# The set of top-level keys that the entity store always carries on every
# entity, regardless of template. The types are the *Python* types the
# LLM-facing mapper produces (see ``EntityMapper.to_agent`` and
# ``AgentEntity``). ``creation_date`` and ``edit_date`` are ISO-8601 UTC
# strings or ``None`` — they are intentionally ``str | None`` in
# ``AgentEntity`` so a missing server-side value is not silently rendered
# as ``"1970-01-01T00:00:00Z"``.
_TOP_LEVEL_KEY_INFO: list[tuple[str, str]] = [
    ("shared_id", "str"),
    ("title", "str"),
    ("template_name", "str"),
    ("metadata", "dict[str, Any]"),
    ("language", "str"),
    ("published", "bool | None"),
    ("creation_date", "str (ISO-8601 UTC) | None"),
    ("edit_date", "str (ISO-8601 UTC) | None"),
]

# Friendly type names per AgentPropertyType, used in the per-property
# shape table. Keep these short — they go into the prompt.
_PROPERTY_TYPE_LABEL: dict[AgentPropertyType, str] = {
    AgentPropertyType.TEXT: "text",
    AgentPropertyType.MARKDOWN: "markdown",
    AgentPropertyType.NUMERIC: "numeric",
    AgentPropertyType.DATE: "date",
    AgentPropertyType.MULTI_DATE: "multidate",
    AgentPropertyType.DATE_RANGE: "daterange",
    AgentPropertyType.MULTI_DATE_RANGE: "multidaterange",
    AgentPropertyType.SELECT: "select (thesaurus label or None)",
    AgentPropertyType.MULTI_SELECT: "multiselect (list of thesaurus labels)",
    AgentPropertyType.LINK: "link ({label, url} or None)",
    AgentPropertyType.GEO_LOCATION: "geolocation ([lat, lon] or None)",
    AgentPropertyType.IMAGE: "image (str URL or None)",
    AgentPropertyType.MEDIA: "media (str URL or None)",
    AgentPropertyType.GENERATED_ID: "generatedid (str)",
    AgentPropertyType.RELATIONSHIP: "relationship (list of {shared_id, title})",
    AgentPropertyType.PREVIEW: "preview (template-only; auto-rendered primary document)",
    AgentPropertyType.NESTED: "nested (template-only parent group; no direct entity value)",
}

# Per-type, the canonical Python shape the mapper emits. Used in the
# per-property shape table to tell the agent what to expect on read and
# what to send back on write (the round-trip contract).
_PROPERTY_VALUE_SHAPE: dict[AgentPropertyType, str] = {
    AgentPropertyType.TEXT: "str",
    AgentPropertyType.MARKDOWN: "str",
    AgentPropertyType.NUMERIC: "int | float",
    AgentPropertyType.DATE: "str (ISO date 'YYYY-MM-DD') | None",
    AgentPropertyType.MULTI_DATE: "list[str] (ISO dates)",
    AgentPropertyType.DATE_RANGE: "{from: str, to: str} | None",
    AgentPropertyType.MULTI_DATE_RANGE: "list[{from: str, to: str}]",
    AgentPropertyType.SELECT: "str (thesaurus label) | None",
    AgentPropertyType.MULTI_SELECT: "list[str] (thesaurus labels)",
    AgentPropertyType.LINK: "{label: str, url: str} | None",
    AgentPropertyType.GEO_LOCATION: "[lat, lon] | None",
    AgentPropertyType.IMAGE: "str | None",
    AgentPropertyType.MEDIA: "str | None",
    AgentPropertyType.GENERATED_ID: "str",
    AgentPropertyType.RELATIONSHIP: "list[{shared_id: str, title: str}]",
    AgentPropertyType.PREVIEW: "None (template-only; never present in entity metadata)",
    AgentPropertyType.NESTED: "None (template-only parent group; never present as its own metadata key)",
}

# A conservative cap on the size of the rendered shape block. The
# orchestrator's delegation message is itself subject to a request
# budget; a 1 000-line table would crowd the user's task. The cap is
# applied to the rendered string and the block is truncated with a
# note when exceeded.
DEFAULT_SHAPE_BLOCK_CHAR_CAP = 6000


def _value_type_name(value: Any) -> str:
    """Best-effort Python-type name for an arbitrary metadata value.

    Used as a fallback when the template is not in the schema store —
    we still want the agent to know ``e['metadata']['author']`` is a
    string, not a dict. The label is short and intentionally
    approximate: ``"list[dict]"`` rather than ``"list[dict[str, Any]]"``.
    """
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return type(value).__name__
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        if not value:
            return "list (empty)"
        head = value[0]
        if isinstance(head, dict):
            return "list[dict]"
        if isinstance(head, (list, tuple)) and len(head) == 2 and all(isinstance(x, (int, float)) for x in head):
            return "list[[lat, lon]]"
        return f"list[{type(head).__name__}]"
    if isinstance(value, dict):
        # Try the common geolocation shape: {lat, lon} -> "[lat, lon]".
        if "lat" in value and "lon" in value:
            return "[lat, lon]"
        if "label" in value and "url" in value:
            return "{label, url}"
        if "from" in value or "to" in value:
            return "{from, to}"
        return "dict"
    return type(value).__name__


def _format_sample(value: Any) -> str:
    """Render a short, readable string for a sample metadata value.

    The sample is meant to give the agent a *concrete* feel for the
    shape, not a copy-pasteable value. Long strings are clipped;
    non-primitive values are rendered with ``repr`` capped at 60 chars.
    """
    if value is None:
        return "None"
    if isinstance(value, str):
        if len(value) <= 40:
            return repr(value)
        return repr(value[:37] + "...")
    if isinstance(value, (int, float, bool)):
        return repr(value)
    if isinstance(value, list):
        if not value:
            return "[]"
        head = value[0]
        if isinstance(head, dict) and "shared_id" in head and "title" in head:
            return f"[{len(value)} related: {head.get('title', '?')!r}, ...]"
        return f"list[{len(value)}]"
    if isinstance(value, dict):
        if "label" in value and "url" in value:
            return f"{{label={value['label']!r}, url={value['url']!r}}}"
        if "lat" in value and "lon" in value:
            return f"[{value['lat']}, {value['lon']}]"
        if "from" in value or "to" in value:
            return f"{{from={value.get('from')!r}, to={value.get('to')!r}}}"
        return "{...}"
    return repr(value)[:60]


def _is_empty_value(value: Any) -> bool:
    """A value is "empty" when it would not satisfy a non-empty filter.

    An empty list, an empty string, a ``None``, and a daterange that is
    fully empty are all "empty" in the LLM-facing sense — they are the
    shapes the mapper produces for a property the entity never set.
    """
    if value is None:
        return True
    if isinstance(value, str) and value == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _has_metadata_key(entity_dict: dict[str, Any], key: str) -> bool:
    """True iff the entity dict has ``key`` in its metadata with a
    non-empty value. We deliberately do NOT use ``is None`` here —
    Uwazi can store ``""`` for unset values, and the agent must learn
    to treat both as "missing"."""
    metadata = entity_dict.get("metadata") or {}
    if not isinstance(metadata, dict):
        return False
    return key in metadata and not _is_empty_value(metadata[key])


def _format_nullability(non_empty: int, total: int) -> str:
    """Render the "missing on N of M entities" tag for a metadata key."""
    if total == 0:
        return "no data"
    if non_empty == total:
        return "always set"
    if non_empty == 0:
        return "always missing"
    return f"missing on {total - non_empty}/{total}"


def _per_template_property_rows(
    template: AgentTemplate | None,
    sample_values_by_key: dict[str, Any],
    present_counts: dict[str, int],
    total: int,
) -> list[str]:
    """Render the per-property table for one template.

    When ``template`` is known, type and flags come from the schema.
    When the schema is missing, the type is inferred from the first
    non-None value and is marked ``(inferred)``.
    """
    rows: list[str] = []
    keys = sorted(sample_values_by_key.keys() | (present_counts.keys()))
    for key in keys:
        sample = sample_values_by_key.get(key)
        non_empty = present_counts.get(key, 0)
        nullability = _format_nullability(non_empty, total)
        if template is not None:
            prop = next((p for p in template.properties if p.name == key), None)
            if prop is not None:
                type_name = _PROPERTY_VALUE_SHAPE.get(prop.type, prop.type.value)
                flags: list[str] = []
                if prop.required:
                    flags.append("required")
                if prop.use_as_filter:
                    flags.append("filter")
                if prop.show_in_card:
                    flags.append("card")
                flag_str = f" [{', '.join(flags)}]" if flags else ""
                rows.append(f"    - {key} ({type_name}){flag_str} — {nullability}; sample: {_format_sample(sample)}")
                continue
        # No template, or no matching prop on the template.
        type_name = _value_type_name(sample)
        rows.append(f"    - {key} ({type_name}, inferred) — {nullability}; sample: {_format_sample(sample)}")
    return rows


def build_entity_store_shape_block(
    entity_store: EntityStore,
    schema_templates: dict[str, AgentTemplate] | None = None,
    char_cap: int = DEFAULT_SHAPE_BLOCK_CHAR_CAP,
) -> str:
    """Render the entity-store shape block for the Python sub-agent.

    Args:
        entity_store: The session entity store holding the entities the
            Python agent is about to iterate.
        schema_templates: Optional mapping of ``template_name ->
            AgentTemplate``. When provided, per-property types and flags
            are pulled from the schema; otherwise they are inferred
            from the store itself. The orchestrator gets this from
            :class:`SchemaStore`.
        char_cap: Hard cap on the rendered string. When the block
            would exceed this, it is truncated with a note pointing at
            ``run_python_code`` (the only place the agent can read
            individual entities).

    Returns:
        A multi-line string. Returns an empty string when the store is
        empty (the agent has nothing to process).
    """
    entities = [e.model_dump() for e in entity_store.entities]
    if not entities:
        return ""

    schema_templates = schema_templates or {}
    total = len(entities)

    # Per-template counts (use Counter; cheap and stable order via sort).
    template_counter: Counter[str] = Counter(e.get("template_name", "") or "<unknown>" for e in entities)

    # Top-level key presence / nullability across the whole store.
    top_level_rows: list[str] = []
    for key, type_label in _TOP_LEVEL_KEY_INFO:
        non_empty = sum(1 for e in entities if key in e and not _is_empty_value(e.get(key)))
        nullability = _format_nullability(non_empty, total)
        top_level_rows.append(f"  - {key} ({type_label}) — {nullability}")

    # Per-template, walk entities of that template and collect metadata
    # keys, sample values (first non-None), and presence counts.
    lines: list[str] = ["Entity store shape (pre-computed for you):"]
    lines.append("")
    lines.append(f"Total entities: {total}")
    lines.append("Per template:")
    for template_name, count in sorted(template_counter.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  - {template_name}: {count}")
    lines.append("")
    lines.append("Per-entity top-level keys (always present on every entity):")
    lines.extend(top_level_rows)
    lines.append("")
    lines.append("Notes on top-level keys:")
    lines.append(
        "  - `creation_date` and `edit_date` are ISO-8601 UTC strings "
        "(e.g. '2024-03-15T10:22:33Z') or `None`. Use these for "
        "'first/last added', 'oldest/newest' questions. "
        "`None` means Uwazi did not record a date."
    )
    lines.append(
        "  - `published` is a READ-ONLY mirror of Uwazi's stored flag "
        "and does NOT control visibility. To publish/unpublish, use "
        "the `publish_entities` / `unpublish_entities` helpers."
    )
    lines.append(
        "  - `metadata` is a dict whose keys match the entity's "
        "template properties. Keys not in the template (e.g. legacy "
        "or unknown properties) are dropped at read time — they are "
        "not in the dict. Required properties that the entity never "
        "set are ALSO absent from the dict: read with "
        "`e['metadata'].get('key')` (never `e['metadata']['key']`)."
    )

    # Per-template metadata tables.
    lines.append("")
    lines.append("Per-template metadata shape:")
    for template_name, count in sorted(template_counter.items(), key=lambda kv: (-kv[1], kv[0])):
        template_entities = [e for e in entities if (e.get("template_name") or "") == template_name]
        # Collect union of metadata keys.
        all_keys: set[str] = set()
        for e in template_entities:
            md = e.get("metadata") or {}
            if isinstance(md, dict):
                all_keys.update(md.keys())
        sample_values: dict[str, Any] = {}
        present_counts: dict[str, int] = {k: 0 for k in all_keys}
        for e in template_entities:
            md = e.get("metadata") or {}
            if not isinstance(md, dict):
                continue
            for k in all_keys:
                if _has_metadata_key(e, k):
                    present_counts[k] += 1
                    if k not in sample_values:
                        sample_values[k] = md.get(k)
        template = schema_templates.get(template_name)
        lines.append(f"  Template '{template_name}' ({count} entities):")
        rows = _per_template_property_rows(template, sample_values, present_counts, len(template_entities))
        if rows:
            lines.extend(rows)
        else:
            lines.append("    - (no metadata keys observed in the store)")

    # Earliest and latest by creation_date. The agent can answer
    # "first/last added" questions from this directly, no need to sort
    # the store.
    lines.append("")
    lines.append("Time range (by `creation_date`):")
    dated = [e for e in entities if e.get("creation_date")]
    if not dated:
        lines.append("  - No entity in the store has a `creation_date`.")
    else:
        dated_sorted = sorted(dated, key=lambda e: e["creation_date"])
        earliest = dated_sorted[0]
        latest = dated_sorted[-1]
        lines.append(
            f"  - Earliest: title={earliest.get('title', '?')!r}, "
            f"shared_id={earliest.get('shared_id', '?')}, "
            f"creation_date={earliest.get('creation_date')}"
        )
        lines.append(
            f"  - Latest:   title={latest.get('title', '?')!r}, "
            f"shared_id={latest.get('shared_id', '?')}, "
            f"creation_date={latest.get('creation_date')}"
        )
        lines.append(
            "  - To find any other time-based ordering (e.g. "
            "'last 5 added', 'oldest with template X'), sort on "
            "`creation_date` in your script. The values above tell "
            "you the store is fully time-ordered or not."
        )

    # Footer pointing at the only safe way to inspect a single entity
    # at runtime — the agent can do it, but it costs output budget.
    lines.append("")
    lines.append(
        "Need a property the shape block does not list? You can still "
        "inspect one entity with `entities[0]['metadata'].keys()` — "
        "this shape block is a *summary*, not a guarantee. Templates "
        "and entity data can drift (legacy entities may have keys not "
        "in the current template); trust the runtime value, not the "
        "schema, when they disagree."
    )

    text = "\n".join(lines)
    if len(text) > char_cap:
        text = (
            text[: char_cap - 80] + "\n... [entity-store shape block truncated; inspect "
            "`entities[0]['metadata'].keys()` for the rest]\n"
        )
    return text
