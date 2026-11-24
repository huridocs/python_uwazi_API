"""Value pickers for the entity window: a relationship's targets and a select's value.

Both pickers persist the operator's choice through the SAME mechanism a
multiselect uses (``set_selected_values`` → the row's ``selected_values``), so
"the operator unchecks a proposed value and adds a new one" is one write path
for every pickable property type.

* A ``relationship`` is always multi-valued: its picker lists the target
  entities' TITLES (never bare sharedIds), with the property's candidate list
  resolved up front.
* A ``select`` holds exactly ONE value: its picker is single-choice and stores
  a one-element selection.

Neither picker carries a search box: the detail window is where a proposal is
reviewed, and the candidates it can offer are already listed. A search field
there only added a second, slower way to find the same values.
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from nicegui import background_tasks, context, ui

from uwazi_admin_agent.domain.relationship_values import (
    relationship_options,
)
from uwazi_admin_agent.domain.select_values import ALL_VALUES_REJECTED_REASON
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus
from uwazi_admin_agent.drivers.web.run_service import (
    relationship_candidates,
    relationship_titles_for,
)
from uwazi_admin_agent.drivers.web.session import credentials_for_instance, page_instance_url

# How many candidates the picker lists. The relationship catalog is read once
# per (template, property, language); there is no per-search narrowing.
RELATIONSHIP_CANDIDATE_LIMIT: int = 50


def _editable(suggestion: Suggestion, runnable: bool) -> bool:
    """Whether the value mechanism may still change this row.

    A row discarded BY the value mechanism stays editable (picking a value back
    is the review loop); every other non-runnable state, and an EDITED row (its
    edit wins over the values), is read-only.
    """
    return (runnable or suggestion.discard_reason == ALL_VALUES_REJECTED_REASON) and (
        suggestion.status is not SuggestionStatus.EDITED
    )


def _persist(state: dict[str, Any], suggestion: Suggestion, selected: list[str] | None) -> None:
    """Persist one row's explicit value selection, then refresh page + window."""
    from .entity_window import _set_selection_by_id

    if suggestion.id is None:
        return
    background_tasks.create(
        _set_selection_by_id(state, suggestion.id, selected, context.client),
        name=f"select values {suggestion.id}",
    )


def _label_with_recommended(title: str, recommended: bool) -> str:
    """The picker's display text: the title, marked when the script proposed it (pure)."""
    return f"{title} (recommended)" if recommended else title


def relationship_picker(
    state: dict[str, Any],
    suggestion: Suggestion,
    titles: dict[str, str],
    runnable: bool,
) -> None:
    """The relationship row's target picker: titles, add and remove (UI).

    The picker's value is the list of target ``sharedId``s the Run will write.
    Unchecking a proposed target drops it; checking another adds it — the
    selected set is persisted verbatim, so the Run writes exactly what the
    operator validated. There is deliberately no free-text search row: the
    candidate list already carries the property's targets, and the detail
    window is for reviewing a value, not for browsing the instance.
    """
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    template_name = suggestion.template_name
    property_name = suggestion.property_name
    language = suggestion.language

    def _catalog() -> list[tuple[str, str]]:
        if credentials is None:
            return []
        return relationship_candidates(
            url,
            template_name,
            property_name,
            credentials.user,
            credentials.password,
            language=language,
            limit=RELATIONSHIP_CANDIDATE_LIMIT,
        )

    try:
        catalog = _catalog()
    except Exception as exc:  # noqa: BLE001 — a candidate read never blocks the window
        logger.warning("could not list relationship candidates: {}", exc)
        catalog = []
    options = relationship_options(
        suggestion.proposed_value,
        suggestion.rejected_values,
        suggestion.selected_values,
        catalog,
        titles,
    )
    selected = [option["id"] for option in options if option["selected"]]
    picker = ui.select(
        {option["id"]: _label_with_recommended(option["label"], option["recommended"]) for option in options},
        multiple=True,
        value=selected,
        label="Related entities",
        on_change=lambda e, s=suggestion: _persist(
            state,
            s,
            list(e.value) if isinstance(e.value, list) else [],
        ),
    ).props("dense outlined use-chips")
    picker.classes("w-full")
    if not _editable(suggestion, runnable):
        picker.disable()
    if not catalog:
        ui.label("Could not list this property's candidate entities — only the values already proposed are listed.").classes(
            "text-caption text-grey-7"
        )


def single_select_picker(
    state: dict[str, Any],
    suggestion: Suggestion,
    available: list[str],
    selected: list[str],
    runnable: bool,
) -> None:
    """The select row's picker: ONE thesaurus value (UI).

    A ``select`` holds a single value, so the picker is single-choice and
    persists a one-element selection (``[]`` when cleared). The options are the
    proposal first, then the property's whole thesaurus, so the operator can
    replace the proposed value with any other.
    """
    from uwazi_admin_agent.domain.select_values import labels_of

    options: list[str] = []
    for label in [*selected, *labels_of(suggestion.proposed_value), *available]:
        if label and label not in options:
            options.append(label)
    chosen = options[0] if selected else None
    picker = ui.select(
        {label: label for label in options},
        value=chosen,
        clearable=True,
        label="Value",
        on_change=lambda e, s=suggestion: _persist(
            state,
            s,
            [e.value] if isinstance(e.value, str) and e.value else [],
        ),
    ).props("dense outlined")
    picker.classes("w-full")
    if not _editable(suggestion, runnable):
        picker.disable()
    if not available:
        ui.label("Could not read this property's thesaurus — only the recommended value is listed.").classes(
            "text-caption text-grey-7"
        )


def select_catalog(suggestion: Suggestion) -> list[str]:
    """The property's thesaurus labels (``[]`` when unreachable)."""
    from uwazi_admin_agent.domain.select_values import catalog_labels

    from .entity_window import _select_catalog_map

    return catalog_labels(_select_catalog_map(suggestion))


def relationship_titles(suggestions: list[Suggestion]) -> dict[str, str]:
    """``{sharedId: title}`` for every target named by these rows (best effort)."""
    from uwazi_admin_agent.domain.relationship_values import shared_ids_of

    ids: list[str] = []
    for suggestion in suggestions:
        for value in (suggestion.proposed_value, suggestion.current_value, suggestion.selected_values):
            for shared_id in shared_ids_of(value):
                if shared_id not in ids:
                    ids.append(shared_id)
    if not ids:
        return {}
    url = page_instance_url()
    credentials = credentials_for_instance(url)
    if credentials is None:
        return {}
    return relationship_titles_for(
        url,
        ids,
        credentials.user,
        credentials.password,
        next((s.language for s in suggestions if s.language), None),
    )


__all__ = [
    "RELATIONSHIP_CANDIDATE_LIMIT",
    "relationship_picker",
    "relationship_titles",
    "select_catalog",
    "single_select_picker",
]
