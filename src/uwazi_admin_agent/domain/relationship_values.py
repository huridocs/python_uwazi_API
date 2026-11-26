"""Relationship-property values: shared ids, display titles, and selections (pure).

A ``relationship`` metadata property stores ``[{"value": <sharedId>}]`` entries.
Those ids are what the script records and what Uwazi writes, but they are
meaningless to the operator reviewing a task: the review surfaces must show the
TARGET ENTITY'S TITLE. Selection works exactly like a multiselect (relationship
is always multi-valued), so the accepted-value rules of
:mod:`uwazi_admin_agent.domain.select_values` apply to the id list.

Everything here is pure: no instance reads, no UI. The title map is supplied by
the caller (the web service resolves titles once per page/window).
"""

from __future__ import annotations

from typing import Any

RELATIONSHIP_TYPE: str = "relationship"

# Property types whose value is a SET the operator can pick from. ``select`` is
# single-valued by Uwazi's schema; the other two are multi-valued.
SELECT_TYPE: str = "select"
PICKABLE_TYPES: frozenset[str] = frozenset({SELECT_TYPE, "multiselect", RELATIONSHIP_TYPE})


def shared_ids_of(value: Any) -> list[str]:
    """The related ``sharedId`` strings a relationship value carries, in order.

    Accepts the on-disk envelope ``[{"value": id}]``, the read shape
    ``[{"shared_id": id, "title": ...}]`` / ``[{"sharedId": id}]``, a bare id
    string, a list of ids, and ``None``. Duplicates are collapsed (first
    occurrence wins) because Uwazi stores one entry per related entity.
    """
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    ids: list[str] = []
    for item in items:
        candidate: Any = item
        if isinstance(item, dict):
            candidate = item.get("value", item.get("shared_id", item.get("sharedId")))
        if not isinstance(candidate, str):
            continue
        candidate = candidate.strip()
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


def embedded_titles(value: Any) -> dict[str, str]:
    """``{sharedId: title}`` for the titles a relationship value ALREADY carries (pure).

    Uwazi's raw read shape is ``[{"value": <id>, "label": <title>}]``, so the
    entity's current value names its own targets. Using that map before any
    lookup keeps the review table readable even when the title lookup is
    unavailable (an unreachable instance, a page whose catalog read failed).
    A ``{"shared_id", "title"}`` dict is accepted too — that is the shape the
    entity API's read view uses.
    """
    items = value if isinstance(value, (list, tuple)) else [value]
    found: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        shared_id = str(item.get("value") or item.get("shared_id") or item.get("sharedId") or "").strip()
        title = str(item.get("title") or item.get("label") or "").strip()
        if shared_id and title and title != shared_id:
            found.setdefault(shared_id, title)
    return found


def relationship_title(shared_id: str, titles: dict[str, str] | None = None) -> str:
    """The display text for one related entity (pure): its title, else its id.

    Falling back to the id keeps an unresolvable target visible and
    diagnosable instead of rendering an empty gap.
    """
    title = (titles or {}).get(shared_id, "")
    return title or shared_id


def render_relationship(value: Any, titles: dict[str, str] | None = None) -> str:
    """Render a relationship value as ``"Title, Title"`` (pure).

    ``—`` for an empty value, matching every other metadata renderer. A title
    the VALUE embeds wins only where the caller's map has none: an explicit
    lookup is the fresher answer (an entity may have been renamed).
    """
    ids = shared_ids_of(value)
    if not ids:
        return "—"
    known = dict(embedded_titles(value))
    known.update({key: title for key, title in (titles or {}).items() if title})
    return ", ".join(relationship_title(shared_id, known) for shared_id in ids)


def relationship_selection(proposal: Any, rejected: list[str], selected: list[str] | None) -> list[str]:
    """The related ids a relationship row currently writes (pure).

    Same rule as a multiselect (:func:`~uwazi_admin_agent.domain.select_values.effective_selection`):
    an explicit ``selected`` list wins — an EMPTY list means "nothing" — and
    otherwise the proposal minus ``rejected``. ``value`` shapes are normalized
    through :func:`shared_ids_of`, so an envelope, a read-shape dict, or a bare
    id list all work.
    """
    if selected is not None:
        chosen: list[str] = []
        for shared_id in shared_ids_of(selected):
            if shared_id not in chosen:
                chosen.append(shared_id)
        return chosen
    dropped = set(rejected)
    return [shared_id for shared_id in shared_ids_of(proposal) if shared_id not in dropped]


def relationship_options(
    proposal: Any,
    rejected: list[str],
    selected: list[str] | None,
    catalog: list[tuple[str, str]],
    titles: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The relationship picker's ordered options, each
    ``{"id", "label", "recommended", "selected"}`` (pure).

    Order: the proposal's ids first (recommended), then the catalog the
    instance offers for the property, then any selected id in neither set (so a
    stored value the catalog does not list stays visible and removable).
    ``label`` is the entity title, falling back to the shared id.

    The selection rule matches a multiselect's: an explicit ``selected`` list
    wins (an EMPTY list means "nothing"), otherwise the proposal minus
    ``rejected``.
    """
    known: dict[str, str] = dict(titles or {})
    for shared_id, title in catalog:
        known.setdefault(shared_id, title)
    proposal_ids = shared_ids_of(proposal)
    current = relationship_selection(proposal, rejected, selected)
    ordered: list[str] = []
    # EVERY proposed id stays listed (even a rejected one), so the operator can
    # put it back — exactly like a multiselect's proposed chips.
    for shared_id in proposal_ids:
        if shared_id not in ordered:
            ordered.append(shared_id)
    for shared_id, _title in catalog:
        if shared_id not in ordered:
            ordered.append(shared_id)
    for shared_id in current:
        if shared_id not in ordered:
            ordered.append(shared_id)
    chosen = set(current)
    proposed = set(proposal_ids)
    return [
        {
            "id": shared_id,
            "label": relationship_title(shared_id, known),
            "recommended": shared_id in proposed,
            "selected": shared_id in chosen,
        }
        for shared_id in ordered
    ]


def single_value(property_type: str | None, values: list[str]) -> list[str]:
    """``values`` narrowed to what Uwazi can store for ``property_type`` (pure).

    A ``select`` holds exactly ONE value, so an explicit multi-pick (or a
    legacy selection list) collapses to its first entry; every other pickable
    type keeps the whole list. Order is preserved.
    """
    if property_type == SELECT_TYPE:
        return values[:1]
    return values


__all__ = [
    "PICKABLE_TYPES",
    "RELATIONSHIP_TYPE",
    "SELECT_TYPE",
    "embedded_titles",
    "relationship_options",
    "relationship_selection",
    "relationship_title",
    "render_relationship",
    "shared_ids_of",
    "single_value",
]
