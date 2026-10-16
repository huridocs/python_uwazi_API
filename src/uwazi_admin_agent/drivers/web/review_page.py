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

The table's **Language** column is part of a row's identity: Uwazi stores text
and richtext as one value PER LANGUAGE while a select or a date has none, so a
translation task records the same entity once per language and each of those is
its own row, checkbox and Run. A change with no language at all (a select, a
title, a delete) is its own row labelled "—" rather than being repeated into
every language. Running the ``es`` row writes ONLY the Spanish text: the apply
path merges the entity's other languages back in, because Uwazi replaces a
property's whole value list on write.

The table's **Status** column is the Run state, per entity: "runned" once every
recorded row of that entity is APPLIED, "pending" while any row is left (the
label and its color come from the same aggregate the row already carries, so
the column costs no extra query).

The table's **Uwazi** column opens the entity's public page on the controlled
instance in a NEW tab (a plain anchor, as in the results window) in the row's
own language. It appears only for ops whose entity already exists there —
``update``/``delete`` — and only when ``UWAZI_URL`` is configured; the URL is
built from the row's sharedId with no extra request.

Write timing: the selection scan never mutates Uwazi — it runs AUTOMATICALLY
right after generation completes (the selection script cannot call the LLM, so
it needs no approval); only "Accept selected"/Run writes (through the apply
run); Reject only sets a row status. The selection is re-read from the table on
EVERY action (``table.selected``) — ``_reload_table`` reassigns ``table.rows``,
so a cached selection goes stale.

Selection count: the action bar's Run button reads ``Run (N)``, updated on the
table's ``selection`` event (NiceGUI's ``on_select`` — the one event Quasar's
QTable emits for a checkbox change; ``update:selection`` is not a real event
and never fires). Reloading the rows prunes the selection to what is still
visible, so the count never names rows the operator cannot see.

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
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, REVIEW_PAGE_SIZE
from uwazi_admin_agent.domain.llm_budget import format_money
from uwazi_admin_agent.domain.suggestion import (
    PredictionAttemptStatus,
    Suggestion,
    SuggestionFilter,
)
from uwazi_admin_agent.drivers.web.review_view import (
    MATCH_COLORS,
    STATUS_COLORS,
    entity_row,
    filter_from_controls,
    match_label,
    prune_selection,
    run_button_label,
    run_confirmation,
    truncate,
)
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_suggestion_store,
    current_attempt,
    get_entity_rows,
    get_entity_segmentation,
    get_entity_suggestions,
    get_run,
    get_suggestion_counts,
    get_suggestion_entity_languages,
    get_suggestion_properties,
    resolve_entity_document,
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
        "op": "",
        "property": "",
        "match": "",
        "text": "",
        "loading": False,
    }

    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.button(icon="arrow_back", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")
            ui.label(f"Review — {detail.run_id}").classes("text-h6 q-ml-sm")
            ui.badge(detail.status.value).props("color=grey-7").classes("q-ml-sm")
        with ui.row().classes("items-center"):
            ui.button("Back to tasks", on_click=lambda: ui.navigate.to("/app")).props("flat color=secondary")

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-7xl"):
            _summary_row(state)
            _filter_bar(state)
            _action_bar(state)
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
        ui.select(
            {"": "any status", **{s: s for s in STATUS_COLORS}},
            value=state["status"],
            label="Status",
            on_change=lambda e: _set_filter(state, "status", e.value or ""),
        ).props("dense outlined").classes("min-w-[140px]")
        ui.select(
            {
                "": "any op",
                "update": "update",
                "create": "create",
                "delete": "delete",
                "publish": "publish",
                "unpublish": "unpublish",
                "set_publish_status": "set_publish_status",
                "create_relationships": "create_relationships",
            },
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
    state[key] = value
    state["offset"] = 0
    _refresh_all(state)


def _reset_filters(state: dict[str, Any]) -> None:
    state.update({"status": "", "op": "", "property": "", "match": "", "text": "", "offset": 0})
    _refresh_all(state)


def _current_filter(state: dict[str, Any], *, accepted_only: bool = False) -> SuggestionFilter:
    return filter_from_controls(
        status=state["status"],
        op=state["op"],
        property_name=state["property"],
        match=state["match"],
        text=state["text"],
        offset=state["offset"],
        limit=REVIEW_PAGE_SIZE,
        accepted_only=accepted_only,
    )


# --- actions -----------------------------------------------------------------


def _selected_entities(state: dict[str, Any]) -> list[tuple[str, str | None]]:
    """The checked rows as ``(sharedId, language)`` pairs (read LIVE from the table).

    A table row is an entity IN ONE LANGUAGE, so a translation task contributes
    two independent pairs and the operator can run one of them. ``language`` is
    ``None`` for the language-independent group, which is exactly how the apply
    path addresses it. ``_reload_table`` reassigns ``table.rows``, so the
    selection is never cached: every action re-reads it at click time.
    """
    table = state.get("_table")
    if table is None or table.is_deleted:
        return []
    selected = getattr(table, "selected", None) or []
    pairs: list[tuple[str, str | None]] = []
    for row in selected:
        if not isinstance(row, dict):
            continue
        shared_id = row.get("shared_id")
        if not isinstance(shared_id, str):
            continue
        language = row.get("language") or None
        if (shared_id, language) not in pairs:
            pairs.append((shared_id, language))
    return pairs


def _action_bar(state: dict[str, Any]) -> None:
    """The selection-driven action bar (the checked rows carry the count)."""
    with ui.row().classes("w-full items-center gap-2 q-mb-sm"):
        state["_run_button"] = ui.button(
            run_button_label(0),
            icon="play_arrow",
            on_click=lambda: _run_selected(state, _selected_entities(state), _selected_ops(state)),
        ).props("dense color=primary")
        state["_select_all_button"] = ui.button(
            "Select all on page",
            icon="select_all",
            on_click=lambda: _select_all(state),
        ).props("dense flat")


def _select_all(state: dict[str, Any]) -> None:
    """Check every row currently on the page."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    table.selected = list(table.rows)
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


def _selected_ops(state: dict[str, Any]) -> list[str]:
    """The ops of the currently checked rows (the Run dialog's wording)."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return []
    return _ops_of_rows(list(getattr(table, "selected", None) or []))


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

    Columns: Entity (title, opens the entity window), Template, Ops, Status
    (runned/pending), Actions (Run / Edit), and Open — a small icon that opens
    the entity's public page on the controlled Uwazi instance in a new tab,
    shown only for rows that already exist there (``update``/``delete``; see
    ``entity_open_url``). No sharedId or change-count columns — the rows are
    precomputed actions and the Run heuristic executes them.
    """
    columns = [
        {"name": "title", "label": "Entity", "field": "title", "align": "left", "sortable": False},
        {"name": "language", "label": "Language", "field": "language", "align": "left", "sortable": False},
        {"name": "template", "label": "Template", "field": "template", "align": "left", "sortable": False},
        {"name": "ops", "label": "Ops", "field": "ops", "align": "left", "sortable": False},
        {"name": "run_status", "label": "Status", "field": "run_status", "align": "left", "sortable": False},
        {"name": "actions", "label": "Actions", "field": "actions", "align": "center", "sortable": False},
        {"name": "open", "label": "Uwazi", "field": "url", "align": "center", "sortable": False},
    ]
    table = ui.table(
        rows=[],
        columns=columns,
        # entity+language, not sharedId: a translation task's two rows are the
        # SAME entity, and each one has to be its own checkable/selectable row.
        row_key="row_key",
        selection="multiple",
        pagination={"rowsPerPage": 0},
        on_select=lambda e: _refresh_selection_counts(state),
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
    table.add_slot(
        "body-cell-title",
        '<q-td :props="props"><a href="#" class="text-primary" '
        "@click.prevent=\"$parent.$emit('open_entity', props.row)\">{{ props.row.title }}</a></q-td>",
    )
    table.add_slot(
        "body-cell-actions",
        '<q-td :props="props" class="text-no-wrap">'
        '<q-btn dense flat icon="play_arrow" color="primary" title="Run this row (revertable; only its language)" '
        "@click=\"$parent.$emit('run_entity', props.row)\" />"
        '<q-btn dense flat icon="edit" color="teal" title="Open the entity window (see/edit its rows)" '
        "@click=\"$parent.$emit('edit_entity', props.row)\" />"
        "</q-td>",
    )
    # A plain anchor (not a q-btn emit): the browser opens the entity's public
    # page on the controlled instance in a NEW tab, leaving the review page and
    # its selection untouched. The URL rides on the row (None -> no icon), for
    # the same reason as the colors above.
    table.add_slot(
        "body-cell-open",
        '<q-td :props="props" class="text-no-wrap">'
        '<a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener" '
        'title="Open this entity in Uwazi"><q-icon name="open_in_new" size="16px" color="primary" /></a>'
        "</q-td>",
    )
    table.add_slot(
        "body-cell-language",
        '<q-td :props="props"><q-badge v-if="props.row.language" color="teal-1" '
        'class="q-mr-xs">{{ props.row.language }}</q-badge>'
        '<span v-else class="text-grey-6">—</span></q-td>',
    )
    table.on("open_entity", lambda e: _open_entity_window(state, *_entity_of(_row_of(e))))
    table.on(
        "run_entity",
        lambda e: _run_selected(state, [_entity_of(_row_of(e))], _ops_of_rows([_row_of(e)])),
    )
    table.on("edit_entity", lambda e: _open_entity_window(state, *_entity_of(_row_of(e))))
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
        table = state.get("_table")
        if table is not None and not table.is_deleted:
            table.rows = [entity_row(item, base_url) for item in rows]
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
    """Update the selection-driven buttons' texts from the LIVE table selection."""
    table = state.get("_table")
    if table is None or table.is_deleted:
        return
    selected = getattr(table, "selected", None) or []
    button = state.get("_run_button")
    if button is not None and not button.is_deleted:
        button.text = run_button_label(len(selected))


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


def _reason_dialog(message: str, on_submit: Any) -> None:
    """Confirm dialog with an optional reason (the reason feeds the revisit loop)."""
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            ui.label(message).classes("text-body1")
            reason_input = ui.textarea("Reason (optional — used to improve the prompt)").classes("w-full")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dialog.close).props("color=grey-7 flat")
                ui.button(
                    "Discard",
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
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                _entity_header(state, dialog, label, shared_id, [s.op for s in suggestions], language)
                with ui.row().classes("w-full grow no-wrap items-stretch").style("min-height: 0"):
                    document_pane = ui.column().classes("grow").style("min-width: 50%; height: 100%")
                    with ui.column().classes("grow").style("min-width: 45%; height: 100%; overflow-y: auto"):
                        _entity_suggestions(state, suggestions)
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
) -> None:
    """The window's title row: Run this row (its language only), or Close."""
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(label).classes("text-h6")
        with ui.row().classes("items-center no-wrap"):
            ui.button(
                "Run this row",
                icon="play_arrow",
                on_click=lambda: _run_selected(state, [(shared_id, language)], ops, [language] if language else None),
            ).props("flat dense color=primary")
            ui.button("Close", icon="close", on_click=lambda: _close_entity_window(state)).props("flat dense")


def _entity_suggestions(state: dict[str, Any], suggestions: list[Suggestion]) -> None:
    """The right-hand panel: the precomputed rows the Run heuristic will write.

    Read-only view (see the row's op, property, current and proposed values,
    and the operator's edit when one was saved via the inline editor); the
    commit happens through the header's Run button, not per-row buttons.
    """
    for suggestion in suggestions:
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(suggestion.property_name or suggestion.op).classes("text-subtitle2")
                ui.badge(match_label(suggestion.match)).props(f"color={MATCH_COLORS[match_label(suggestion.match)]}")
            if suggestion.op in ("update", "create"):
                ui.label(f"Current: {truncate(suggestion.current_value, 200)}").classes("text-body2 text-grey-7")
                ui.label(f"Proposed: {truncate(suggestion.proposed_value, 200)}").classes("text-body2")
            if suggestion.edited_value is not None:
                ui.label(f"Edited: {suggestion.edited_value}").classes("text-body2 text-teal")


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
