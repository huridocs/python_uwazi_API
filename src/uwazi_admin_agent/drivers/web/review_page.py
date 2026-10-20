"""The review page: the entity table, its filters, and the entity window.

One page per run (``/review/{run_id}``), separate from the runs table because
reviewing is a different job from running: the selection scan produced one row
per AFFECTED ENTITY IN ONE LANGUAGE, the operator checks a subset, and the
actions act on the checked rows — "Run" executes each checked row's values with
the deterministic apply heuristic (no LLM at write time), "Revert" undoes them.

Scale rules this module obeys:

- the table reads ``get_entity_rows`` (SQL GROUP BY + LIMIT/OFFSET in the store);
- the header cards come from ``counts`` (SQL ``GROUP BY``), counted over the same
  entity-language groups, so "Total" always equals the table's own entity total;
- the property dropdown comes from ``property_names`` (SQL ``DISTINCT``);
- selection-driven actions resolve to an explicit (sharedId, language) id list
  read live from the table — never a Python pass over the whole set.

The table's **Language** column is part of a row's identity: Uwazi keeps one
entity-language ROW per translation (a text/richtext property holds a single
value on it) while a select or a date has none, so a translation task records the
same entity once per language and each of those is its own row, checkbox and Run.
A change with no language at all (a select, a title, a delete) is its own row
labelled "—" rather than being repeated into every language. Running the ``es``
row writes ONLY the Spanish row: the apply path issues one write per language,
and the ``en`` row is a different entity row this write never touches.

The table's **Status** column is the Run state, per entity: "runned" once every
recorded row of that entity is APPLIED, "pending" while any row is left (the
label and its color come from the same aggregate the row already carries, so
the column costs no extra query).

The **Entity** column carries the open-in-Uwazi link as an icon at the END of
the (trimmed, ellipsised) title rather than as a column of its own: a plain
anchor opens the entity's public page on the controlled instance in a NEW tab
(the row's own language), only for ops whose entity already exists there —
``update``/``delete`` — and only when ``UWAZI_URL`` is configured; the URL is
built from the row's sharedId with no extra request. Titles are cut in the row
payload as well as in the cell, because a document title is unbounded and a cell
that sizes to its content is what made the table scroll sideways.

The entity window's rows are headed by the property's human LABEL ("Document
Ref"), resolved once per template from ``/api/templates``, with the raw property
name beneath it; nothing is resolved when the instance is unreachable, and the
names are shown exactly as before.

Write timing: the selection scan never mutates Uwazi — it runs AUTOMATICALLY
right after generation completes (the selection script cannot call the LLM, so
it needs no approval); only "Accept selected"/Run writes (through the apply
run); Reject only sets a row status. The selection is re-read from the table on
EVERY action (``table.selected``) — ``_reload_table`` reassigns ``table.rows``,
so a cached selection goes stale.

Discard: the operator can refuse to execute an operation — "the end user does
not want this" — with the Discard button in the table's Actions cell (and the
bulk Discard over the checked selection). A discard MARKS the operation's rows
(DISCARDED, with the reason the operator typed): a Run accepts only PENDING and
REVERTED rows, so a discarded operation is never written, and the Undo button
brings it back to the review queue. The rows ALREADY written keep their status
(a discard says what must not be executed; un-writing is the Revert's job), and
the "Discarded" filter reads both halves of the decision — "discarded" (the
refused operations) and "not discarded" (what is left to execute) — which the
plain Status filter cannot express. The Status and Discarded filters exclude
each other: "discarded" plus "not discarded" would be an empty table.

Selection count: the action bar's Run button reads ``Run (N)``, updated on the
table's ``selection`` event (NiceGUI's ``on_select`` — the one event Quasar's
QTable emits for a checkbox change; ``update:selection`` is not a real event
and never fires). There are two selection scopes: the header checkbox selects
the visible PAGE (reloading prunes the selection to those rows), while the
"Select all N results" button selects every row matching the current filter
across ALL pages. In whole-result mode ``Run (N)`` counts the filter total minus
the rows the operator explicitly unchecked (the exclusions are row keys, so a
translation's two languages stay independent). Run resolves those pairs from the
store at click time; page flips never truncate the selection.

One window at a time: the entity window is a single dialog that is closed and
rebuilt on every open, because its content (the PDF) resolves in a background
task and a second click would otherwise stack dialogs.

Documents: the window resolves the entity's primary document in the ROW's
language and falls back to the instance's other configured languages (an entity
usually holds its PDF in the language it was uploaded in, not the one being
translated). When no language has one, the window shows the document text
instead of mounting a viewer that would render nothing.

One attempt per run: there is no attempt selector. ``current_attempt`` (the
run's newest prediction pass) is resolved inside the read functions, so the
page has no attempt state to carry; the header's Spent/budget card and the
progress poll read that same attempt, which is why they still describe the
pass that produced the rows on screen.

The header carries the controlled instance's URL as a link — the same widget the
runs table shows — because the rows being reviewed are about to be written there,
and every failure in a background task (generation, apply) is logged server-side
so ``docker logs`` shows the traceback a toast cannot.
"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, REVIEW_PAGE_SIZE
from uwazi_admin_agent.domain.drift import DriftStatus
from uwazi_admin_agent.domain.llm_budget import format_money
from uwazi_admin_agent.domain.suggestion import (
    PROPERTY_OPS,
    PredictionAttemptStatus,
    Suggestion,
    SuggestionFilter,
    SuggestionStatus,
    entity_language_key,
)
from uwazi_admin_agent.drivers.web.review_view import (
    ACTIONS_COLUMN,
    DRIFT_STATUS_COLORS,
    LANGUAGE_COLUMN,
    MATCH_COLORS,
    OP_COLORS,
    OPS_COLUMN,
    STATUS_COLORS,
    STATUS_COLUMN,
    TEMPLATE_COLUMN,
    TITLE_COLUMN,
    bulk_discard_label,
    bulk_revert_label,
    counterpart_filter,
    discard_confirmation,
    drift_banner_text,
    drift_banner_time,
    entity_row,
    filter_from_controls,
    full_value,
    match_label,
    property_caption,
    property_heading,
    prune_selection,
    revert_entity_confirmation,
    rows_excluding_keys,
    run_button_label,
    run_confirmation,
    select_all_label,
    selection_count,
    truncate,
    unique_entities,
)
from uwazi_admin_agent.drivers.web.run_service import (
    _current_attempt_number,
    apply_outcomes,
    build_backup_store,
    build_suggestion_store,
    check_drift,
    current_attempt,
    discard_rows,
    get_drift_state,
    get_entity_rows,
    get_entity_segmentation,
    get_entity_suggestions,
    get_run,
    get_suggestion_counts,
    get_suggestion_entity_languages,
    get_suggestion_properties,
    property_labels_for,
    refresh_baseline_after_run,
    resolve_entity_document,
    restore_rows,
    revert_entities_selection,
    revert_entity_rows,
    uwazi_base_url,
)


def _modal_header(dialog: Any, title: str) -> None:
    """Modal title row with a top-right Close button (matches app.py's helper)."""
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(title).classes("text-h6")
        ui.button("Close", icon="close", on_click=dialog.close).props("flat")


def _store() -> Any:
    """The shared suggestion store (SQL-side status writes for row actions)."""
    return build_suggestion_store()


@ui.page("/review/{run_id}")
def review_page(run_id: str) -> None:
    """Render the review page for one run (login required)."""
    if not app.storage.user.get("user"):
        ui.navigate.to("/")
        return
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    _build_page(run_id)


def _build_page(run_id: str) -> None:
    try:
        detail = get_run(run_id)
    except Exception as exc:  # noqa: BLE001 — a missing run shows an error, never a crash
        with ui.column().classes("w-full items-center justify-center min-h-screen"):
            ui.label(f"Cannot open review for {run_id!r}: {exc}").classes("text-body1 text-negative")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat")
        return

    state: dict[str, Any] = {
        "run_id": run_id,
        "offset": 0,
        "total": 0,
        "status": "",
        "discarded": "",
        "op": "",
        "property": "",
        "match": "",
        "text": "",
        "loading": False,
        # Whole-result selection (the button, not the page header checkbox):
        # every row matching the current filter, across every page, minus any
        # row the operator explicitly unchecks afterwards. ``_excluded`` holds
        # row_key strings (entity+language), so a translation's two rows stay
        # independent.
        "_select_all": False,
        "_excluded": set(),
        # Set while a filter control is reset programmatically: assigning a
        # control's value fires its own change handler, and the reset must not
        # re-enter the filter logic (the State and Discarded filters reset each
        # other).
        "_suppress_filter_events": False,
    }

    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")
            ui.label(f"Review — {detail.run_id}").classes("text-h6 q-ml-sm")
            ui.badge(detail.status.value).props("color=grey-7").classes("q-ml-sm")
        with ui.row().classes("items-center"):
            # The controlled instance this run writes to — the same header link
            # the runs table carries, so the operator always knows which instance
            # the rows they are about to write belong to.
            instance_url = uwazi_base_url()
            if instance_url:
                ui.icon("link", color="secondary").classes("q-mr-xs")
                ui.link(instance_url, instance_url, new_tab=True).classes("text-white")
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-7xl"):
            _summary_row(state)
            _filter_bar(state)
            _action_bar(state)
            _drift_banner(state)
            _change_table(state)

    if current_attempt(run_id) is None:
        ui.notify(
            "No scan recorded yet — the automatic scan right after generation may still be running",
            type="info",
        )

    ui.timer(3.0, lambda: _poll_progress(state))


# --- header ------------------------------------------------------------------


def _summary_row(state: dict[str, Any]) -> None:
    holder = ui.row().classes("w-full items-center gap-3 q-mb-sm")
    state["_summary"] = holder
    _render_summary(state)


def _render_summary(state: dict[str, Any]) -> None:
    holder = state.get("_summary")
    if holder is None or holder.is_deleted:
        return
    counts = get_suggestion_counts(state["run_id"])
    attempt = current_attempt(state["run_id"])
    holder.clear()
    with holder:
        # The status cards count ENTITIES (entity-language groups, the table's own
        # row identity), not the property rows behind them: "Total" must equal the
        # "of N entities" the table footer shows, so a single entity carrying 17
        # property rows reads 1 here and 1 there. Each status card is that filter's
        # entity count — a group whose rows span statuses counts in each, exactly as
        # picking that status in the dropdown below would.
        _card("Total", f"{counts.get('total', 0):,}")
        _card("Pending", f"{counts.get('pending', 0):,}", "grey")
        _card("Accepted", f"{counts.get('accepted', 0):,}", "green")
        _card("Edited", f"{counts.get('edited', 0):,}", "teal")
        _card("Discarded", f"{counts.get('discarded', 0):,}", "red")
        _card("Applied", f"{counts.get('applied', 0):,}", "blue")
        _card("Reverted", f"{counts.get('reverted', 0):,}", "indigo")
        if attempt is not None:
            _card(
                "Spent / budget",
                f"{format_money(attempt.budget.call_cost(attempt.spent.input_tokens, attempt.spent.output_tokens))} "
                f"/ {format_money(attempt.budget.max_cost)}",
            )


def _card(label: str, value: str, color: str = "primary") -> None:
    with ui.card().classes("q-pa-sm").style("min-width: 118px"):
        ui.label(label).classes("text-caption text-grey-7")
        ui.label(value).classes(f"text-subtitle1 text-bold text-{color}")


# --- filters -----------------------------------------------------------------


def _filter_bar(state: dict[str, Any]) -> None:
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_status_select"] = (
            ui.select(
                {"": "any status", **{s: s for s in STATUS_COLORS}},
                value=state["status"],
                label="Status",
                on_change=lambda e: _set_filter(state, "status", e.value or ""),
            )
            .props("dense outlined")
            .classes("min-w-[140px]")
        )
        # The Discarded filter: the operator marks whole operations as discarded
        # ("the end user does not want this executed") and then works on what is
        # left, so the filter has to read BOTH halves of that decision — the
        # Status dropdown above can only say "discarded", never "not discarded".
        state["_discarded_select"] = (
            ui.select(
                {"": "any discard state", "no": "not discarded", "yes": "discarded"},
                value=state["discarded"],
                label="Discarded",
                on_change=lambda e: _set_filter(state, "discarded", e.value or ""),
            )
            .props("dense outlined")
            .classes("min-w-[160px]")
        )
        ui.select(
            {"": "any op", **{op: op for op in OP_COLORS}},
            value=state["op"],
            label="Op",
            on_change=lambda e: _set_filter(state, "op", e.value or ""),
        ).props("dense outlined").classes("min-w-[150px]")
        properties = get_suggestion_properties(state["run_id"])
        ui.select(
            {"": "any property", **{p: p for p in properties}},
            value=state["property"],
            label="Property",
            on_change=lambda e: _set_filter(state, "property", e.value or ""),
        ).props("dense outlined").classes("min-w-[150px]")
        ui.select(
            {"": "any match", "exact": "exact", "mismatch": "mismatch", "new": "new"},
            value=state["match"],
            label="Match",
            on_change=lambda e: _set_filter(state, "match", e.value or ""),
        ).props("dense outlined").classes("min-w-[130px]")
        ui.input(
            "Search title / sharedId",
            value=state["text"],
            on_change=lambda e: _set_filter(state, "text", e.value or ""),
        ).props("dense outlined clearable").classes("grow")
        ui.button("Mismatches only", icon="rule", on_click=lambda: _set_filter(state, "match", "mismatch")).props(
            "flat dense"
        )
        ui.button("Reset", icon="restart_alt", on_click=lambda: _reset_filters(state)).props("flat dense")


def _set_filter(state: dict[str, Any], key: str, value: Any) -> None:
    if state.get("_suppress_filter_events"):
        # A programmatic reset of another control (see _reset_filter_widget).
        return
    state[key] = value
    # The Status and the Discarded filter both speak about the discarded rows,
    # so they exclude each other: "discarded" + "not discarded" would be an
    # empty table nobody can read. Picking one resets the other.
    if value:
        other = counterpart_filter(key)
        if other:
            _reset_filter_widget(state, other)
    state["offset"] = 0
    _reset_global_selection(state)
    _refresh_all(state)


def _reset_filter_widget(state: dict[str, Any], key: str) -> None:
    """Clear one filter and its control (the two discard filters exclude each other).

    Assigning a control's value fires its own change handler, which would
    re-enter :func:`_set_filter` and refresh the table a second time — so the
    events are suppressed while the control is reset. The filter's state is set
    here, because the suppressed handler does not do it.
    """
    state[key] = ""
    widget = state.get(f"_{key}_select")
    if widget is None or widget.is_deleted:
        return
    state["_suppress_filter_events"] = True
    try:
        widget.value = ""
    finally:
        state["_suppress_filter_events"] = False


def _reset_filters(state: dict[str, Any]) -> None:
    state.update(
        {
            "status": "",
            "discarded": "",
            "op": "",
            "property": "",
            "match": "",
            "text": "",
            "offset": 0,
        }
    )
    for key in ("status", "discarded"):
        widget = state.get(f"_{key}_select")
        if widget is not None and not widget.is_deleted:
            state["_suppress_filter_events"] = True
            try:
                widget.value = ""
            finally:
                state["_suppress_filter_events"] = False
    _reset_global_selection(state)
    _refresh_all(state)


def _current_filter(state: dict[str, Any], *, accepted_only: bool = False) -> SuggestionFilter:
    return filter_from_controls(
        status=state["status"],
        discarded=state["discarded"],
        op=state["op"],
        property_name=state["property"],
        match=state["match"],
        text=state["text"],
        offset=state["offset"],
        limit=REVIEW_PAGE_SIZE,
        accepted_only=accepted_only,
    )


# --- actions -----------------------------------------------------------------


def _pairs_of_rows(rows: list[Any]) -> list[tuple[str, str | None]]:
    """The ``(sharedId, language)`` identity of checked table rows (pure).

    A table row is an entity IN ONE LANGUAGE, so a translation task contributes
    two independent pairs. ``language`` is ``None`` for the language-independent
    group, which is exactly how the apply path addresses it.
    """
    pairs: list[tuple[str, str | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        shared_id = row.get("shared_id")
        if not isinstance(shared_id, str):
            continue
        language = row.get("language") or None
        if (shared_id, language) not in pairs:
            pairs.append((shared_id, language))
    return pairs


def _all_selected_rows(state: dict[str, Any]) -> list[Any]:
    """Every entity-language group matching the filter, minus the exclusions.

    The whole-result mode cannot read the ids from the table (it holds only the
    current page), so Run resolves them from the store through the SAME filter
    the page used. The aggregate rows are fetched in one page sized to ``total``
    because Run also needs their ops for the confirmation wording.
    """
    total = int(state.get("total") or 0)
    if total <= 0:
        return []
    f = _current_filter(state).model_copy(update={"offset": 0, "limit": total})
    rows, _ = get_entity_rows(state["run_id"], f)
    excluded = set(state.get("_excluded") or ())
    return [row for row in rows if entity_language_key(row.shared_id, row.language) not in excluded]


def _ops_of_entities(rows: list[Any]) -> list[str]:
    """Distinct ops across aggregate entity rows, first-appearance order (pure)."""
    ops: list[str] = []
    for row in rows:
        for op in row.ops:
            if op not in ops:
                ops.append(op)
    return ops


def _selection_snapshot(state: dict[str, Any]) -> tuple[list[tuple[str, str | None]], list[str]]:
    """What Run would act on right now: the selected pairs and their ops.

    Page selection reads the table's own checkboxes; whole-result selection
    re-reads the matching groups from the store so a page flip or a filter the
    operator cannot see does not silently truncate the selection.
    """
    if state.get("_select_all"):
        rows = _all_selected_rows(state)
        return [(row.shared_id, row.language) for row in rows], _ops_of_entities(rows)
    table = state.get("_table")
    if table is None or table.is_deleted:
        return [], []
    selected = list(getattr(table, "selected", None) or [])
    return _pairs_of_rows(selected), _ops_of_rows(selected)


def _run_from_state(state: dict[str, Any]) -> None:
    """Open the Run confirmation for the selection the button's count describes."""
    selection, ops = _selection_snapshot(state)
    _run_selected(state, selection, ops)


def _action_bar(state: dict[str, Any]) -> None:
    """The selection-driven action bar (the checked rows carry the count)."""
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_run_button"] = ui.button(
            run_button_label(0),
            icon="play_arrow",
            on_click=lambda: _run_from_state(state),
        ).props("dense color=primary")
        state["_bulk_revert_button"] = ui.button(
            bulk_revert_label(0),
            icon="undo",
            on_click=lambda: _revert_from_state(state),
        ).props("dense flat color=indigo")
        # The bulk Discard: marking the checked operations as "will not be
        # executed" one by one is not workable on a table of thousands, so the
        # selection carries it too.
        state["_bulk_discard_button"] = ui.button(
            bulk_discard_label(0),
            icon="block",
            on_click=lambda: _discard_from_state(state),
        ).props("dense flat color=negative")
        state["_select_all_button"] = ui.button(
            select_all_label(0, False),
            icon="select_all",
            on_click=lambda: _toggle_select_all(state),
        ).props("dense flat")


def _revert_from_state(state: dict[str, Any]) -> None:
    """Open the bulk-Revert confirmation for the selection the button's count describes."""
    selection, _ops = _selection_snapshot(state)
    if not selection:
        ui.notify("Nothing selected", type="warning")
        return
    entities = unique_entities(selection)

    def _confirm() -> None:
        # The client is captured HERE, inside the click's slot context (the same
        # pattern _run_rows uses): the background task starts with an empty slot
        # stack, so every UI call it makes must re-enter the client explicitly.
        client = context.client
        background_tasks.create(_revert_selected(state, entities, client), name=f"revert {state['run_id']}")

    title = f"Revert {len(entities)} entit{'' if len(entities) == 1 else 'ies'}"
    message = (
        f"Restore {len(entities)} entit{'' if len(entities) == 1 else 'ies'} to the backup the run made "
        "BEFORE any of its writes. ALL languages of each entity are restored (they share one backup); "
        "only the checked entities are reverted."
    )
    _simple_confirm(title, message, _confirm)


async def _revert_selected(state: dict[str, Any], entities: list[str], client: Any) -> None:
    """Revert the selected entities (one restore each), then refresh the page."""
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    try:
        outcome = await revert_entities_selection(state["run_id"], [(sid, None) for sid in entities], user, password)
        with client:
            ui.notify(
                f"Revert done — {outcome.summary()}",
                type="positive" if not outcome.failures else "warning",
                multi_line=True,
            )
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("bulk revert failed")
        with client:
            ui.notify(f"Revert failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _refresh_all(state)


# --- discard ------------------------------------------------------------------


def _discard_from_state(state: dict[str, Any]) -> None:
    """Open the bulk-Discard dialog for the selection the button's count describes.

    The dialog asks for the reason too: the operator discards an operation
    because the end user does not want it executed, and WHY is worth keeping
    with the rows (it is stored as the rows' discard reason).
    """
    selection, _ops = _selection_snapshot(state)
    if not selection:
        ui.notify("Nothing selected", type="warning")
        return

    def _submit(reason: str | None) -> None:
        # The client is captured HERE, inside the click's slot context (the same
        # pattern _run_rows uses): the background task starts with an empty slot
        # stack, so every UI call it makes must re-enter the client explicitly.
        client = context.client
        background_tasks.create(
            _discard_rows(state, selection, reason, client),
            name=f"discard {state['run_id']}",
        )

    title, message = discard_confirmation(len(selection))
    _reason_dialog(
        message,
        _submit,
        title=title,
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want these executed)",
    )


def _discard_entity_clicked(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Mark ONE operation as discarded (the row's own Discard button).

    The operation is the table's row: an entity in one language. A translation
    task's two languages are two operations, so discarding the ``es`` row
    leaves the ``en`` one untouched.
    """
    shared_id, language = _entity_of(row)
    pairs = [(shared_id, language)]

    def _submit(reason: str | None) -> None:
        client = context.client
        background_tasks.create(
            _discard_rows(state, pairs, reason, client),
            name=f"discard {shared_id}",
        )

    _reason_dialog(
        "Mark this operation as discarded? It will NOT be executed by a Run. The reason is stored with it.",
        _submit,
        title="Discard operation",
        confirm_label="Discard",
        reason_label="Reason (optional — why the end user does not want this executed)",
    )


async def _discard_rows(
    state: dict[str, Any],
    pairs: list[tuple[str, str | None]],
    reason: str | None,
    client: Any,
) -> None:
    """Mark the given operations as discarded, then refresh the page."""
    try:
        count = await asyncio.to_thread(discard_rows, state["run_id"], pairs, reason)
        with client:
            ui.notify(
                f"Discarded {count} row(s) — a Run will not execute them",
                type="positive",
                multi_line=True,
            )
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("discard failed")
        with client:
            ui.notify(f"Discard failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _refresh_all(state)


def _restore_entity_clicked(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Undo the discard of ONE operation (its rows go back to pending).

    No dialog: an undo only ever widens what a Run may execute, so it is the
    safe direction — the operator can discard it again.
    """
    shared_id, language = _entity_of(row)
    client = context.client
    background_tasks.create(
        _restore_rows(state, [(shared_id, language)], client),
        name=f"restore {shared_id}",
    )


async def _restore_rows(state: dict[str, Any], pairs: list[tuple[str, str | None]], client: Any) -> None:
    """Return the given operations' discarded rows to pending, then refresh."""
    try:
        count = await asyncio.to_thread(restore_rows, state["run_id"], pairs)
        with client:
            ui.notify(f"Restored {count} row(s) — they can be run again", type="positive")
    except Exception as exc:  # noqa: BLE001 — the toast reports; the page never crashes
        logger.exception("restore failed")
        with client:
            ui.notify(f"Restore failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _refresh_all(state)


# --- per-row revert ----------------------------------------------------------


def _backup_dates_for(rows: list[Any], run_id: str) -> dict[str, Any]:
    """``sharedId -> backup captured_at`` for the page's entities (local files only).

    One read attempt per entity (best-effort): an absent snapshot file means
    the run never wrote to that entity, so no tooltip date. Errors are logged,
    never raised — a missing date must not break the table render.
    """
    dates: dict[str, Any] = {}
    store = build_backup_store()
    for item in rows:
        try:
            snapshot = store.load_snapshot(run_id, item.shared_id)
        except FileNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 — a broken file degrades to no tooltip
            logger.warning("could not load backup for tooltip shared_id={}: {}", item.shared_id, exc)
            continue
        dates[item.shared_id] = snapshot.captured_at
    return dates


async def _revert_entity_clicked(state: dict[str, Any], row: dict[str, Any]) -> None:
    """Revert one entity (all its languages) to its single backup, then refresh.

    Confirm first: the restore overwrites the entity's rows in every language
    the task touched, not just the clicked row's language.
    """
    shared_id, _language = _entity_of(row)

    def _confirm() -> None:
        client = context.client
        background_tasks.create(_revert_entity_rows(state, shared_id, client), name=f"revert {shared_id}")

    title, message = revert_entity_confirmation(shared_id, row.get("revert_tooltip") or "")
    _simple_confirm(title, message, _confirm)


async def _revert_entity_rows(state: dict[str, Any], shared_id: str, client: Any) -> None:
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    try:
        info = await revert_entity_rows(state["run_id"], shared_id, user, password)
        with client:
            ui.notify(
                f"Reverted {shared_id} to its backup"
                + (f" ({info.backup_created_at:%Y-%m-%d %H:%M})" if info.backup_created_at else ""),
                type="positive",
            )
    except Exception as exc:  # noqa: BLE001 — the toast reports, the page never crashes
        logger.exception("per-entity revert failed")
        with client:
            ui.notify(f"Revert failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _refresh_all(state)


# --- drift banner ------------------------------------------------------------


def _drift_banner(state: dict[str, Any]) -> None:
    """The status line above the table: what the last drift check concluded.

    Renders the STORED verdict (or "not checked yet"); the open-page auto-check
    and the manual button update it. One row: an icon + the text + (right) the
    check-again button.
    """
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_drift_icon"] = ui.icon("sync", color="grey-6").classes("text-body1")
        state["_drift_label"] = ui.label(drift_banner_text(status="unknown", detail=None)).classes(
            "text-body2 text-grey-7 grow"
        )
        state["_drift_button"] = ui.button(
            "Check drift",
            icon="sync",
            on_click=lambda: background_tasks.create(
                _check_drift_now(state, client=context.client),
            ),
        ).props("dense flat")

    # The verdict's text is stored per run: apply it immediately, then let the
    # open-page auto-check refresh it (a fresh page should not show a stale
    # 'valid' while Uwazi moved — the check replaces the text in place).
    stored = get_drift_state(state["run_id"])
    _apply_drift_state(state, stored, checking=stored is None)
    if stored is None or stored.status is DriftStatus.UNKNOWN or stored.checked_by == "sync":
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))
    elif stored.status is not DriftStatus.VALID:
        # A previous page visit already found drift; re-check in the background
        # so the operator sees CURRENT drift rather than yesterday's.
        background_tasks.create(_check_drift_now(state, notify=False, client=context.client))


def _apply_drift_state(state: dict[str, Any], stored: Any, *, checking: bool = False) -> None:
    """Paint one verdict (or the checking glyph) onto the banner elements."""
    label = state.get("_drift_label")
    icon = state.get("_drift_icon")
    if label is None or label.is_deleted:
        return
    if checking:
        label.text = drift_banner_text(status="unknown", detail=None, checking=True)
        if icon is not None and not icon.is_deleted:
            icon.name = "sync"
            icon.classes("animate-spin text-body1")
            icon.color = "blue"
            icon.update()
        return
    if stored is None:
        label.text = drift_banner_text(status="unknown", detail=None)
        return
    label.text = drift_banner_text(
        status=stored.status.value,
        detail=stored.detail,
    ) + drift_banner_time(stored.checked_at)
    color = DRIFT_STATUS_COLORS.get(stored.status.value, "grey-6")
    icon_name = {
        "unknown": "help_outline",
        "valid": "check_circle",
        "drifted": "warning",
        "error": "error",
    }.get(stored.status.value, "help_outline")
    if icon is not None and not icon.is_deleted:
        icon.name = icon_name
        # ``classes(...)`` ADDS by default: without removing the checking
        # stage's ``animate-spin`` here, the verdict icon would keep spinning
        # forever. A fresh class list also drops stale text-color entries so
        # the new verdict's color wins.
        icon.classes(replace="text-body1")
        icon.classes(f"text-{color}")
        icon.update()


async def _check_drift_now(state: dict[str, Any], notify: bool = True, client: Any = None) -> None:
    """Run the drift check in the background thread and repaint the banner.

    ``client`` is captured by the caller INSIDE the page's slot context (the
    same pattern ``_run_rows`` uses): a background task starts with an empty
    slot stack, so every UI call it makes — the banner updates and the
    toasts — must re-enter the client explicitly via ``with client:``.
    """
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        _apply_drift_state(
            state,
            None,
        )
        state["_drift_label"].text = "Drift check failed — not logged in to Uwazi"
        return
    _apply_drift_state(state, None, checking=True)
    try:
        attempt_row = current_attempt(state["run_id"])
        # ``check_drift`` is SYNCHRONOUS (it blocks on Uwazi HTTP calls), so it
        # must run in a worker thread, like every other service call (the
        # apply_outcomes/revert_run pattern) — awaiting it directly raised
        # "object DriftStateRow can't be used in 'await' expression".
        updated = await asyncio.to_thread(
            check_drift,
            state["run_id"],
            user,
            password,
            attempt_row.attempt if attempt_row is not None else 1,
            "auto" if not notify else "manual",
        )
    except Exception as exc:  # noqa: BLE001 — the banner reports; the page never crashes
        logger.exception("drift check failed")
        state["_drift_label"].text = f"Drift check failed — {exc}"
        return
    _apply_drift_state(state, updated)
    if not notify or client is None:
        return
    try:
        with client:
            if updated.status.value == "drifted":
                ui.notify(updated.detail or "Drift detected in Uwazi", type="warning")
            elif updated.status.value == "valid":
                ui.notify("No drift detected — table is current", type="positive")
    except RuntimeError as exc:  # noqa: BLE001 — a disconnected client cannot show a toast
        logger.debug("could not notify drift verdict (client gone): {}", exc)


def _set_table_selection(state: dict[str, Any], rows: list[Any]) -> None:
    """Replace the table's checkbox selection (server-side, no event round-trip)."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    table.selected = list(rows)
    # Table's ``selected`` setter, unlike most NiceGUI props, does not push the
    # change by itself; without this the checkboxes would not follow the mode.
    table.update()


def _clear_selection(state: dict[str, Any], *, clear_page: bool) -> None:
    """Leave whole-result mode and (optionally) uncheck the visible page."""
    state["_select_all"] = False
    state["_excluded"] = set()
    if clear_page:
        _set_table_selection(state, [])
    _refresh_selection_counts(state)


def _reset_global_selection(state: dict[str, Any]) -> None:
    """Drop a whole-result selection when the filter that defined it changes."""
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)


def _toggle_select_all(state: dict[str, Any]) -> None:
    """Select every row matching the filter across pages, or clear the selection."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    if state.get("_select_all"):
        _clear_selection(state, clear_page=True)
        return
    if int(state.get("total") or 0) <= 0:
        return
    state["_select_all"] = True
    state["_excluded"] = set()
    # The current page shows the new mode immediately. Other pages pick it up in
    # _reload_table, which re-applies the same selection after each page load.
    _set_table_selection(state, list(table.rows))
    _refresh_selection_counts(state)


def _on_table_select(state: dict[str, Any], event: Any) -> None:
    """Keep the page checkboxes and the whole-result mode consistent.

    In whole-result mode the header checkbox still means "this page": unchecking
    it excludes that page from the global selection (and re-checking a row puts
    it back). The count therefore stays total-minus-exclusions rather than
    collapsing to the visible page size.
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    if state.get("_select_all"):
        selected = getattr(event, "selection", None)
        if selected is None:
            selected = getattr(table, "selected", None) or []
        selected_keys = {str(row.get("row_key")) for row in selected if isinstance(row, dict)}
        excluded: set[str] = state.setdefault("_excluded", set())
        for row in table.rows:
            if not isinstance(row, dict):
                continue
            key = str(row.get("row_key"))
            if key in selected_keys:
                excluded.discard(key)
            else:
                excluded.add(key)
    _refresh_selection_counts(state)


def _ops_of_rows(rows: list[Any]) -> list[str]:
    """Distinct ops across the given table rows, first-appearance order.

    Read LIVE with the selection: the dialog names the change in Uwazi, so it
    must describe what is checked RIGHT NOW, not what was on the page earlier.
    """
    ops: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        for op in str(row.get("ops") or "").split(","):
            op = op.strip()
            if op and op not in ops:
                ops.append(op)
    return ops


def _run_selected(
    state: dict[str, Any],
    selection: list[tuple[str, str | None]],
    ops: list[str] | None = None,
    languages: list[str] | None = None,
) -> None:
    """Execute the selected rows with the Run heuristic (revertable).

    The Run action IS the acceptance: ``ApplySuggestionsUseCase.apply`` accepts
    the selected rows' PENDING state as it runs them, so the rows the table
    shows (recorded by the planning pass, never individually reviewed) are what
    gets written. ``selection`` is the checked (sharedId, language) pairs, so a
    translation task writes only the languages that were checked. ``ops`` and
    ``languages`` only shape the confirm dialog's wording.
    """
    if not selection:
        ui.notify("Nothing selected", type="warning")
        return

    def _confirm() -> None:
        # The client is captured HERE, inside the click's slot context: the
        # background task starts with an empty slot stack, so every UI call it
        # makes (notify, refresh) must re-enter the client explicitly.
        client = context.client
        background_tasks.create(_run_rows(state, selection, client), name=f"run {state['run_id']}")

    title, message = run_confirmation(len(selection), ops, languages or _languages_of(selection))
    _simple_confirm(title, message, _confirm)


def _languages_of(selection: list[tuple[str, str | None]]) -> list[str]:
    """The distinct languages of a selection, language-independent last (pure).

    The Run dialog names what it is about to write, and for a translation task
    that is "the es text", not "an entity" — so the languages travel with the
    count into the confirmation.
    """
    languages: list[str] = []
    saw_agnostic = False
    for _shared_id, language in selection:
        if language is None:
            saw_agnostic = True
        elif language not in languages:
            languages.append(language)
    if saw_agnostic:
        languages.append("—")
    return languages


async def _run_rows(state: dict[str, Any], selection: list[tuple[str, str | None]], client: Any) -> None:
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if not user or not password:
        with client:
            ui.notify("Not logged in to Uwazi", type="negative")
        return
    try:
        await apply_outcomes(state["run_id"], user, password, selection)
        # The writes bumped Uwazi editDates: re-sync the baseline so the run's
        # own writes are the new reference, not drift.
        refresh_baseline_after_run(state["run_id"], user, password, _current_attempt_number(state["run_id"]))
        with client:
            ui.notify(
                f"Executed — 'Revert' on run {state['run_id']!r} (or Revert task) undoes it",
                type="positive",
                multi_line=True,
            )
    except Exception as exc:  # noqa: BLE001
        logger.exception("run (heuristic apply) failed")
        with client:
            ui.notify(f"Run failed: {exc}", type="negative", multi_line=True)
    finally:
        with client:
            _refresh_all(state)


# --- entity table ------------------------------------------------------------


def _change_table(state: dict[str, Any]) -> None:
    """The ENTITY table: one row per affected entity, with a checkbox column.

    Columns: Entity (the sharedId as plain selectable text, then the title that
    opens the entity window, with the open-in-Uwazi icon at its end), Language,
    Template, Ops, Status (runned/pending) and Actions (Run / Edit). No
    sharedId or change-count columns — the rows are precomputed actions and the
    Run heuristic executes them.

    The widths are explicit (:mod:`review_view`'s column constants) and the
    Entity column clips with an ellipsis: document titles are arbitrarily long,
    and a cell that sizes to its content is what pushed the table wider than its
    card and made every page a horizontal scroll.

    The open-in-Uwazi link is a plain anchor at the END of the title rather than
    its own column (see :func:`entity_open_url` for when the row carries one):
    it is a per-row shortcut to the same entity the title already opens, and as a
    column it cost a full column of width for a 16px icon.
    """
    columns = [
        {"name": "title", "label": "Entity", "field": "title", "align": "left", "sortable": False, "style": TITLE_COLUMN},
        {
            "name": "language",
            "label": "Language",
            "field": "language",
            "align": "left",
            "sortable": False,
            "style": LANGUAGE_COLUMN,
        },
        {
            "name": "template",
            "label": "Template",
            "field": "template",
            "align": "left",
            "sortable": False,
            "style": TEMPLATE_COLUMN,
        },
        {"name": "ops", "label": "Ops", "field": "ops", "align": "left", "sortable": False, "style": OPS_COLUMN},
        {
            "name": "run_status",
            "label": "Status",
            "field": "run_status",
            "align": "left",
            "sortable": False,
            "style": STATUS_COLUMN,
        },
        {
            "name": "actions",
            "label": "Actions",
            "field": "actions",
            "align": "center",
            "sortable": False,
            "style": ACTIONS_COLUMN,
        },
    ]
    table = ui.table(
        rows=[],
        columns=columns,
        # entity+language, not sharedId: a translation task's two rows are the
        # SAME entity, and each one has to be its own checkable/selectable row.
        row_key="row_key",
        selection="multiple",
        pagination={"rowsPerPage": 0},
        on_select=lambda e: _on_table_select(state, e),
    ).classes("w-full")
    # Colors arrive on the row (computed in Python): a Vue slot cannot reach
    # ``window.*``, so looking them up in the template renders empty badges.
    table.add_slot(
        "body-cell-ops",
        '<q-td :props="props"><q-badge v-for="(op, i) in props.row.ops.split(\', \')" :key="op" '
        ':color="props.row.op_colors[i]" class="q-mr-xs">{{ op }}</q-badge></q-td>',
    )
    table.add_slot(
        "body-cell-run_status",
        '<q-td :props="props"><q-badge :color="props.row.run_status_color" '
        'class="q-mr-xs">{{ props.row.run_status }}</q-badge></q-td>',
    )
    # The Entity cell: the sharedId as PLAIN TEXT, then the open-in-Uwazi icon,
    # then the entity title as the entity-window link. The icon sits BETWEEN
    # them (not at the END, not as its own column).
    #
    # Plain text, not links: an anchor is draggable, so a drag over it is a LINK
    # DRAG and never a text selection — that is what made the sharedId so hard
    # to select with the mouse (and what made the whole cell behave like a
    # trigger). The title is a span for the same reason and opens the window
    # through a click handler that steps aside while the operator is selecting
    # text: a drag ends with a click, and a click that opened the dialog would
    # throw the selection away.
    #
    # Three flex items: id, icon, title. The title takes the remaining width and
    # the ellipsis (``min-width: 0`` is what lets it shrink); ``title_full`` is
    # the uncut title for the tooltip, since the cell can only show part of it.
    table.add_slot(
        "body-cell-title",
        '<q-td :props="props">'
        '<div class="row items-center no-wrap" style="width: 100%;">'
        '<span class="text-grey-7 q-mr-xs" style="flex: 0 1 auto; min-width: 0; overflow: hidden; '
        'text-overflow: ellipsis; white-space: nowrap; user-select: text;">{{ props.row.shared_id }}</span>'
        '<a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener" '
        'class="q-mr-xs" style="flex: 0 0 auto;" title="Open this entity in Uwazi" draggable="false">'
        '<q-icon name="open_in_new" size="15px" color="primary" /></a>'
        '<span class="text-primary" style="flex: 1 1 auto; min-width: 0; overflow: hidden; '
        'text-overflow: ellipsis; white-space: nowrap; cursor: pointer; user-select: text;" '
        ':title="props.row.title_full" '
        '@click="$event => { const sel = $event.target.ownerDocument.getSelection(); '
        "if (sel && !sel.isCollapsed && sel.rangeCount && "
        "($event.target.contains(sel.getRangeAt(0).startContainer) || "
        "$event.target.contains(sel.getRangeAt(0).endContainer))) return; "
        "$parent.$emit('open_entity', props.row); }\">{{ props.row.title }}</span>"
        "</div>"
        "</q-td>",
    )
    table.add_slot(
        "body-cell-actions",
        '<q-td :props="props" class="text-no-wrap">'
        '<q-btn v-if="props.row.revertable" dense flat icon="undo" color="indigo" '
        ":title=\"props.row.revert_tooltip || 'Revert this entity to its backup (all its languages)'\" "
        "@click=\"$parent.$emit('revert_entity', props.row)\" />"
        '<q-btn dense flat icon="play_arrow" color="primary" title="Run this row (revertable; only its language)" '
        "@click=\"$parent.$emit('run_entity', props.row)\" />"
        '<q-btn dense flat icon="edit" color="teal" title="Open the entity window (see/edit its rows)" '
        "@click=\"$parent.$emit('edit_entity', props.row)\" />"
        # The discard: the operator refuses to execute this operation ("the end
        # user does not want this"). A discarded row offers the undo instead —
        # the same cell, one button, so the action is where the operator
        # expects it.
        '<q-btn v-if="props.row.discarded" dense flat icon="restore" color="orange" '
        'title="Undo the discard — this operation goes back to the review queue" '
        "@click=\"$parent.$emit('restore_entity', props.row)\" />"
        '<q-btn v-else dense flat icon="block" color="negative" '
        'title="Mark this operation as discarded — it will NOT be executed by a Run" '
        "@click=\"$parent.$emit('discard_entity', props.row)\" />"
        "</q-td>",
    )
    table.add_slot(
        "body-cell-language",
        # A DARK badge: Quasar renders badge text white, so a light palette entry
        # (``teal-1``) put white text on a near-white pill and the language was
        # invisible. ``teal-9`` keeps the tint and restores the contrast.
        '<q-td :props="props"><q-badge v-if="props.row.language" color="teal-9" '
        'class="q-mr-xs">{{ props.row.language }}</q-badge>'
        '<span v-else class="text-grey-6">—</span></q-td>',
    )
    table.on("open_entity", lambda e: _open_entity_window(state, *_entity_of(_row_of(e))))
    table.on(
        "run_entity",
        lambda e: _run_selected(state, [_entity_of(_row_of(e))], _ops_of_rows([_row_of(e)])),
    )
    table.on("edit_entity", lambda e: _open_entity_window(state, *_entity_of(_row_of(e))))
    table.on("revert_entity", lambda e: _revert_entity_clicked(state, _row_of(e)))
    table.on("discard_entity", lambda e: _discard_entity_clicked(state, _row_of(e)))
    table.on("restore_entity", lambda e: _restore_entity_clicked(state, _row_of(e)))
    state["_table"] = table

    with ui.row().classes("w-full items-center justify-between"):
        state["_page_label"] = ui.label("").classes("text-body2 text-grey-7")
        with ui.row().classes("items-center"):
            ui.button(icon="chevron_left", on_click=lambda: _page(state, -1)).props("flat dense")
            state["_page_number"] = ui.label("").classes("text-body2 q-mx-sm")
            ui.button(icon="chevron_right", on_click=lambda: _page(state, 1)).props("flat dense")
    _reload_table(state)


def _row_of(event: Any) -> dict[str, Any]:
    """The clicked row dict out of a table event (NiceGUI sends it as ``args``)."""
    args = event.args
    return args if isinstance(args, dict) else {}


def _entity_of(row: dict[str, Any]) -> tuple[str, str | None]:
    """A table row's ``(sharedId, language)`` identity (pure).

    The row's ``language`` is a string (possibly empty) so Quasar can render it;
    an empty one is the language-independent group, which the apply path
    addresses as ``None``.
    """
    return str(row.get("shared_id") or ""), (row.get("language") or None)


def _page(state: dict[str, Any], direction: int) -> None:
    new_offset = state["offset"] + direction * REVIEW_PAGE_SIZE
    if new_offset < 0 or new_offset >= state["total"]:
        return
    state["offset"] = new_offset
    _reload_table(state)


def _reload_table(state: dict[str, Any]) -> None:
    """Load one page of ENTITY rows (SQL GROUP BY + LIMIT/OFFSET) and update counters.

    The rows carry each entity's public Uwazi URL (read once here, so the table
    costs no extra request for it).
    """
    if state.get("loading"):
        return
    state["loading"] = True
    try:
        rows, total = get_entity_rows(state["run_id"], _current_filter(state))
        state["total"] = total
        base_url = uwazi_base_url()
        # Per-row Revert tooltip: each page reads its entities' backup dates (a
        # local file read per entity — no HTTP). Entities without a backup
        # (never written) carry None and their Revert icon's tooltip falls back
        # to the generic text.
        tooltips = _backup_dates_for(rows, state["run_id"])
        table = state.get("_table")
        if table is not None and not table.is_deleted:
            table.rows = [entity_row(item, base_url, backup_created_at=tooltips.get(item.shared_id)) for item in rows]
            if state.get("_select_all"):
                # Whole-result mode: every page shows its rows checked except the
                # ones the operator explicitly excluded. The count is the filter
                # total minus those exclusions, not the page size.
                _set_table_selection(state, rows_excluding_keys(table.rows, set(state.get("_excluded") or ())))
            else:
                # New rows under the checkboxes: drop the selections that are no
                # longer visible, so the count (and Run) describe what the operator
                # can actually see. A poll reloads the same page, so those survive.
                table.selected = prune_selection(list(getattr(table, "selected", None) or []), table.rows)
            table.update()
        label = state.get("_page_label")
        if label is not None and not label.is_deleted:
            first = state["offset"] + 1 if total else 0
            last = min(state["offset"] + REVIEW_PAGE_SIZE, total)
            label.text = f"Showing {first:,}–{last:,} of {total:,} entities"
        page_number = state.get("_page_number")
        if page_number is not None and not page_number.is_deleted:
            pages = max((total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE, 1)
            page_number.text = f"page {state['offset'] // REVIEW_PAGE_SIZE + 1} / {pages}"
        _refresh_selection_counts(state)
    finally:
        state["loading"] = False


def _refresh_selection_counts(state: dict[str, Any]) -> None:
    """Update the selection-driven buttons' texts from the LIVE selection.

    In whole-result mode the Run count is the filter total minus the rows the
    operator excluded, so it stays correct across page flips (where the table's
    own ``selected`` list only ever holds the visible page).
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    selected = getattr(table, "selected", None) or []
    all_selected = bool(state.get("_select_all"))
    total = int(state.get("total") or 0)
    excluded = len(state.get("_excluded") or ())
    count = selection_count(
        all_selected=all_selected,
        total=total,
        page_selected=len(selected),
        excluded=excluded,
    )
    button = state.get("_run_button")
    if button is not None and not button.is_deleted:
        button.text = run_button_label(count)
    revert_button = state.get("_bulk_revert_button")
    if revert_button is not None and not revert_button.is_deleted:
        revert_button.text = bulk_revert_label(count)
        revert_button.set_enabled(count > 0)
    discard_button = state.get("_bulk_discard_button")
    if discard_button is not None and not discard_button.is_deleted:
        discard_button.text = bulk_discard_label(count)
        discard_button.set_enabled(count > 0)
    select_button = state.get("_select_all_button")
    if select_button is not None and not select_button.is_deleted:
        select_button.text = select_all_label(total, all_selected)
        select_button.set_enabled(all_selected or total > 0)


def _refresh_all(state: dict[str, Any]) -> None:
    """Re-render the summary, the selection buttons, and the current page."""
    try:
        _render_summary(state)
        if state["offset"] >= state["total"] and state["offset"] > 0:
            state["offset"] = max(state["total"] - REVIEW_PAGE_SIZE, 0)
        _reload_table(state)
    except Exception:  # noqa: BLE001 — a refresh failure must not break the page
        logger.exception("review refresh failed")


def _poll_progress(state: dict[str, Any]) -> None:
    """Refresh the header while a pass is RUNNING (progress + spend)."""
    attempt = current_attempt(state["run_id"])
    if attempt is None or attempt.status != PredictionAttemptStatus.RUNNING:
        return
    _render_summary(state)
    _reload_table(state)


# --- row actions -------------------------------------------------------------


# --- dialogs -----------------------------------------------------------------


# --- dialogs -----------------------------------------------------------------


def _simple_confirm(title: str, message: str, on_confirm: Any) -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button("Confirm", color="negative", on_click=lambda: _fire(dialog, on_confirm))
    dialog.open()


def _fire(dialog: Any, on_confirm: Any) -> None:
    dialog.close()
    on_confirm()


def _reason_dialog(
    message: str,
    on_submit: Any,
    *,
    title: str = "",
    confirm_label: str = "Discard",
    reason_label: str = "Reason (optional — used to improve the prompt)",
) -> None:
    """Confirm dialog with an optional reason (the reason is stored with the rows).

    The reason is not decoration: the operator discards an operation because
    the end user does not want it executed, and WHY is kept with the rows so the
    decision can be read back later.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            if title:
                ui.label(title).classes("text-h6")
            ui.label(message).classes("text-body1")
            reason_input = ui.textarea(reason_label).classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button(
                    confirm_label,
                    color="negative",
                    on_click=lambda: _submit_reason(dialog, reason_input, on_submit),
                )
    dialog.open()


def _submit_reason(dialog: Any, reason_input: Any, on_submit: Any) -> None:
    reason = (reason_input.value or "").strip() or None
    dialog.close()
    on_submit(reason)


# --- entity window -----------------------------------------------------------


def _open_entity_window(state: dict[str, Any], shared_id: str, language: str | None = None) -> None:
    """One entity: its primary document beside all its suggestions.

    AT MOST ONE window is ever open. The row click that opens it is not
    instantaneous (the dialog is built, then its document resolves in a
    background task), so an impatient second click used to leave two or three
    dialogs stacked on top of each other. Any window still open is closed
    before the next one is created, which makes "open one, swap it" the only
    possible outcome no matter how fast the operator clicks.
    """
    _close_entity_window(state)
    suggestions = get_entity_suggestions(state["run_id"], shared_id, language)
    if not suggestions:
        ui.notify(f"No suggestions for {shared_id}", type="warning")
        return
    title = suggestions[0].entity_title or shared_id
    label = f"{title} — {shared_id}" + (f" · {language}" if language else "")
    labels = _property_labels(suggestions)
    # A discarded operation says so in its own window too: the operator who
    # opened it needs to know a Run will not execute its rows.
    discarded = all(s.status is SuggestionStatus.DISCARDED for s in suggestions)
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _entity_header(state, dialog, label, shared_id, [s.op for s in suggestions], language, discarded)
                with ui.row().classes("w-full grow no-wrap items-stretch").style("min-height: 0"):
                    document_pane = ui.column().classes("grow").style("min-width: 50%; height: 100%")
                    with ui.column().classes("grow").style("min-width: 45%; height: 100%; overflow-y: auto"):
                        _entity_suggestions(state, suggestions, labels)
                with ui.row().classes("w-full justify-between shrink-0"):
                    with ui.row().classes("items-center"):
                        ui.button(
                            "Previous row",
                            icon="chevron_left",
                            on_click=lambda: _navigate_entity(state, shared_id, language, -1),
                        ).props("flat dense")
                        ui.button(
                            "Next row",
                            icon="chevron_right",
                            on_click=lambda: _navigate_entity(state, shared_id, language, 1),
                        ).props("flat dense")
                    ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat")
    state["_entity_dialog"] = dialog
    dialog.open()
    background_tasks.create(_load_document(state, shared_id, language, document_pane), name=f"document {shared_id}")


def _close_entity_window(state: dict[str, Any]) -> None:
    """Close the entity window if one is open (the one-window guard)."""
    dialog = state.get("_entity_dialog")
    if dialog is not None and not dialog.is_deleted:
        dialog.close()
    state["_entity_dialog"] = None


async def _load_document(state: dict[str, Any], shared_id: str, language: str | None, pane: Any) -> None:
    """Fill the left pane with the entity's PDF viewer, or its document text.

    The decision is made BEFORE rendering: an entity with no uploaded primary
    document gets the segmentation text panel instead of a viewer that would show
    nothing (the plan's no-blank-viewer rule). Both paths degrade to a message
    rather than an empty box.

    The document is resolved in the ROW's language with a fallback to the
    instance's other configured languages (an entity usually holds its PDF in
    the language it was uploaded in, not the one being translated), and the
    viewer is pointed at a fully-built server URL — the viewer never has to
    reconstruct the entity's identity from query params.
    """
    user = app.storage.user.get("user")
    password = app.storage.user.get("password")
    if pane.is_deleted:
        return
    with pane:
        ui.linear_progress(show_value=False).classes("w-full")
        ui.label("Loading the entity's document…").classes("text-body2 text-grey-7")
    if not user or not password:
        _render_pane_message(pane, "Not logged in to Uwazi — cannot show the document.")
        return
    try:
        document = await resolve_entity_document(user, password, shared_id, language)
    except Exception as exc:  # noqa: BLE001 — no document info is not an error state
        logger.info("no primary document for {} ({}): {}", shared_id, language, exc)
        document = None
    if pane.is_deleted:  # the operator moved on while we were resolving
        return
    pane.clear()
    if document is not None:
        with pane:
            ui.element("iframe").props(f"src={_viewer_src(state, shared_id, document.language)}").classes("w-full").style(
                "height: 100%; border: 0"
            )
        return
    await _render_segmentation_fallback(pane, user, password, shared_id, language)


def _viewer_src(state: dict[str, Any], shared_id: str, language: str) -> str:
    """The iframe URL that renders an entity's PDF (pure).

    The document route is passed whole through the viewer's ``src`` parameter,
    URL-encoded: an inner ``&`` would otherwise split into the OUTER query
    string and the viewer would fetch an entity-less URL.
    """
    document_url = (
        f"/review-pdf?run={quote(str(state['run_id']), safe='')}"
        f"&sid={quote(shared_id, safe='')}"
        f"&lang={quote(language, safe='')}"
    )
    return f"/static/pdfjs/review_viewer.html?src={quote(document_url, safe='')}"


async def _render_segmentation_fallback(
    pane: Any, user: str, password: str, shared_id: str, language: str | None = None
) -> None:
    """Show the entity's document TEXT when there is no uploaded primary document."""
    try:
        segmentation = await get_entity_segmentation(user, password, shared_id, language or DUMMY_LANGUAGE)
    except Exception as exc:  # noqa: BLE001 — degrade to a message, never a blank pane
        _render_pane_message(pane, f"No primary document, and no readable document text ({exc}).")
        return
    ordered = sorted(segmentation.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
    text = "\n".join(f"[page {p.page_number}] {p.text}" for p in ordered)
    with pane:
        ui.label(f"Document text — {segmentation.filename} (no uploaded primary PDF)").classes("text-subtitle2 text-grey-7")
        ui.code(text or "(no paragraphs)").classes("w-full").style("white-space: pre-wrap; overflow-y: auto; height: 90%")


def _render_pane_message(pane: Any, message: str) -> None:
    if pane.is_deleted:
        return
    pane.clear()
    with pane:
        ui.label(message).classes("text-body2 text-grey-7")


def _entity_header(
    state: dict[str, Any],
    dialog: Any,
    label: str,
    shared_id: str,
    ops: list[str] | None = None,
    language: str | None = None,
    discarded: bool = False,
) -> None:
    """The window's title row: Run this row (its language only), or Close."""
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(label).classes("text-h6")
        with ui.row().classes("items-center no-wrap"):
            if discarded:
                ui.badge("discarded").props("color=negative").classes("q-mr-sm")
            ui.button(
                "Run this row",
                icon="play_arrow",
                on_click=lambda: _run_selected(state, [(shared_id, language)], ops, [language] if language else None),
            ).props("flat dense color=primary")
            ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat dense")


def _property_labels(suggestions: list[Suggestion]) -> dict[str, str]:
    """The entity's template property labels for the window's headings (cached, best-effort).

    A row names its property by the template's property ``name``
    (``document_ref``); the operator reads it as "Document Ref". The lookup is
    cached per (account, template) behind :func:`property_labels_for`, so it costs
    one ``/api/templates`` read the first time a template's entity is opened and
    nothing after. An unreachable instance yields ``{}`` and the window shows the
    raw names — the same thing it always showed.
    """
    template_name = next((s.template_name for s in suggestions if s.template_name), None)
    if not template_name:
        return {}
    return property_labels_for(template_name, app.storage.user.get("user") or "", app.storage.user.get("password") or "")


def _entity_value_label(text: str, classes: str) -> None:
    """One value line in the entity window, rendered IN FULL (wrapped, newlines kept).

    The review table truncates to keep cells on one line; the detail window must
    not — a ten-line paragraph has to be readable there. ``pre-wrap`` preserves
    the value's own line breaks and ``overflow-wrap`` stops a long word from
    widening the pane.
    """
    ui.label(text).classes(classes).style("white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word")


def _entity_suggestions(state: dict[str, Any], suggestions: list[Suggestion], labels: dict[str, str] | None = None) -> None:
    """The right-hand panel: the precomputed rows the Run heuristic will write.

    Read-only view (see the row's op, property, current and proposed values,
    and the operator's edit when one was saved via the inline editor); the
    commit happens through the header's Run button, not per-row buttons.

    Each card is headed by the property's LABEL — what Uwazi calls it and what
    the operator knows it by — with the raw property name beneath it, so a row is
    both readable and still matchable against the template or a CSV
    (:func:`property_heading` / :func:`property_caption`). ``labels`` is the
    template's label map; without it the heading falls back to the raw name.
    """
    resolved = labels or {}
    shown_merge_sources = False
    for suggestion in suggestions:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("items-start no-wrap").style("min-width: 0"):
                    ui.label(property_heading(suggestion.property_name, suggestion.op, resolved)).classes("text-subtitle2")
                    caption = property_caption(suggestion.property_name, resolved)
                    if caption:
                        ui.label(caption).classes("text-caption text-grey-6")
                ui.badge(match_label(suggestion.match)).props(f"color={MATCH_COLORS[match_label(suggestion.match)]}")
            if suggestion.op in PROPERTY_OPS:
                _entity_value_label(f"Current: {full_value(suggestion.current_value)}", "text-body2 text-grey-7")
                _entity_value_label(f"Proposed: {full_value(suggestion.proposed_value)}", "text-body2")
            if suggestion.edited_value is not None:
                _entity_value_label(f"Edited: {suggestion.edited_value}", "text-body2 text-teal")
            if suggestion.merge_source_ids and not shown_merge_sources:
                shown_merge_sources = True
                ui.label(
                    f"Also deletes {len(suggestion.merge_source_ids)} merged source(s): "
                    f"{truncate(', '.join(suggestion.merge_source_ids), 200)}"
                ).classes("text-body2 text-negative")


def _navigate_entity(state: dict[str, Any], current: str, language: str | None, direction: int) -> None:
    """Move to the previous/next ROW within the current filter's row list.

    Navigation walks (sharedId, language) pairs, so it lands on the next row of
    the table the operator is looking at — for a translation task that means the
    sibling language of the same entity, not a different entity.
    """
    rows = get_suggestion_entity_languages(state["run_id"], _current_filter(state))
    if not rows:
        return
    current_key = (current, language)
    index = rows.index(current_key) if current_key in rows else 0
    target_shared_id, target_language = rows[(index + direction) % len(rows)]
    _open_entity_window(state, target_shared_id, target_language)


__all__ = ["review_page"]
