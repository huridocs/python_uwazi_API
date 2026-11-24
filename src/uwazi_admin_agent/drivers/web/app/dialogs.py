"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

from typing import Any

from nicegui import background_tasks, context, ui

from uwazi_admin_agent.domain.metadata_diff import PropertyChange, build_property_changes
from uwazi_admin_agent.drivers.web import operation_gate

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.run_service import (
    delete_run,
    get_entity_segmentation,
    get_run,
    get_run_audit,
    list_runs,
    rename_run,
)
from uwazi_admin_agent.drivers.web.session import (
    credentials_for_instance,
    page_instance_url,
)
from uwazi_admin_agent.drivers.web.task_dialogs import (
    modal_header,
)

from .broadcast import _broadcast_rows, _run_async
from .state import _ABORT, _busy_run_text, _session_ended


def _confirm_dialog(
    title: str,
    message: str,
    on_confirm: Any,
    success_msg: str,
    instance: str | None = None,
    run_id: str | None = None,
    kind: str = "deleting",
) -> None:
    """Yes/no confirmation dialog; runs ``on_confirm`` (sync or async) on confirm.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    instance = instance or page_instance_url()
    with context.client.layout:
        with ui.dialog() as dialog, ui.card():
            modal_header(dialog, title)
            ui.label(message).classes("text-body1")
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.close())
                ui.button(
                    "Confirm",
                    color="negative",
                    on_click=lambda: _confirm_and_close(dialog, on_confirm, success_msg, instance, run_id, kind),
                )
    dialog.open()


def _rename_dialog(run_id: str) -> None:
    """Prompt for a new task name and rename the run in a background task.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
            dialog.props("persistent")
            modal_header(dialog, f"Rename task — {run_id}")
            name_input = ui.input(
                "New name",
                value=run_id,
                validation={"Required": lambda v: bool(v and v.strip())},
            ).classes("w-full text-h6")
            with ui.row().classes("q-mt-lg w-full justify-end"):
                ui.button("Cancel", on_click=lambda: dialog.close()).props("color=grey-7 flat")
                ui.button(
                    "Rename",
                    icon="edit",
                    on_click=lambda: _rename_confirm(dialog, run_id, name_input),
                )
    dialog.open()


def _error_dialog(run_id: str) -> None:
    """Modal: the run's last error detail plus its full audit trail.

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as the other modals.

    The dialog is maximized (top to bottom): the error detail and audit trail
    share the space under a pinned title, whose top-right Close button is
    always visible without scrolling.
    """
    try:
        detail = get_run(page_instance_url(), run_id)
        records = get_run_audit(page_instance_url(), run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, f"Error details — {run_id}")
                if detail.error:
                    ui.textarea(value=detail.error).classes("w-full grow font-mono text-caption").props(
                        "readonly outlined"
                    ).style("min-height: 0")
                else:
                    ui.label("No error recorded on this run.").classes("text-grey-7")
                with ui.column().classes("w-full shrink-0").style("max-height: 40%; overflow-y: auto"):
                    ui.label("Audit trail").classes("text-subtitle1 q-mt-lg")
                    if not records:
                        ui.label("No audit records.").classes("text-grey-7")
                    else:
                        rows = [
                            {
                                "time": r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                                "step": r.step.value,
                                "op": r.op_kind,
                                "outcome": r.outcome.value,
                                "detail": r.detail or "",
                            }
                            for r in records
                        ]
                        columns = [
                            {"name": "time", "label": "Time (UTC)", "field": "time", "align": "left", "sortable": True},
                            {"name": "step", "label": "Step", "field": "step", "align": "left", "sortable": True},
                            {"name": "op", "label": "Operation", "field": "op", "align": "left", "sortable": True},
                            {"name": "outcome", "label": "Outcome", "field": "outcome", "align": "left", "sortable": True},
                            {"name": "detail", "label": "Detail", "field": "detail", "align": "left", "sortable": False},
                        ]
                        ui.table(
                            rows=rows,
                            columns=columns,
                            row_key="time",
                            pagination={"rowsPerPage": 0},
                        ).classes("w-full")
    dialog.open()


def _format_value(value: Any) -> str:
    """Render an arbitrary manifest value (e.g. a rewired 'before' state) as text."""
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= 200 else text[:200] + "…"


def _entity_url(shared_id: str, language: str | None, base_url: str | None) -> str | None:
    """One entity row's public URL on ``base_url``, or None when it is unknown.

    Uwazi serves an entity at ``/<language>/entity/<sharedId>``; the language
    falls back to ``en`` when the row's locale is unknown. Returns None when
    no base url is given, so the caller omits the link instead of rendering a
    broken one.
    """
    if not base_url:
        return None
    return f"{base_url.rstrip('/')}/{language or 'en'}/entity/{shared_id}"


def _merge_changes(writes: list[Any]) -> list[PropertyChange]:
    """Merge one entity's applied writes into one property list (record order).

    A property touched by several writes keeps the BEFORE of the first write
    that touched it (the pre-run value) and the AFTER of the last (the surviving
    value), so the table shows the net effect of the run rather than each step.
    """
    order: list[str] = []
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    for write in writes:
        for change in build_property_changes(write):
            if change.property not in before:
                order.append(change.property)
                before[change.property] = change.before
            after[change.property] = change.after
    return [
        PropertyChange(
            property=name,
            before=before[name],
            after=after[name],
            changed=before[name] != after[name],
        )
        for name in order
    ]


def _change_summary(changes: list[PropertyChange]) -> str:
    """One-line summary of the changed properties, truncated with a remainder count."""
    changed = [c.property for c in changes if c.changed]
    if not changed:
        return "—"
    shown: list[str] = []
    for name in changed:
        if shown and len(", ".join([*shown, name])) > 60:
            break
        shown.append(name)
    remaining = len(changed) - len(shown)
    text = ", ".join(shown)
    return f"{text} (+{remaining} more)" if remaining else text


def _row_language(identity_language: str | None, writes: list[Any]) -> str | None:
    """The row locale to use for an entity's link and segmentation fetch.

    The identity's language wins. A created entity's identity carries none
    (Uwazi mints the row; we only learn its sharedId), so the write that created
    it supplies the language the script asked for. None when neither knows.
    """
    if identity_language:
        return identity_language
    return next((w.language for w in writes if w.language), None)


def _entity_section(
    title: str,
    entities: list[Any],
    writes_by_sid: dict[str, list[Any]],
    titles: dict[str, str],
    include_restored: bool = False,
    show_changes: bool = True,
    show_segmentation: bool = True,
) -> Any:
    """Render one touch-set section (modified/created/deleted) as a table, if non-empty.

    Rows prefer the entity title over the shared id, carry the controlled
    instance's URL for the open-in-Uwazi icon, and summarize the properties the
    run changed for that entity. The emitted events (``changes``,
    ``segmentation``) are wired by the caller, which owns the run's data.

    ``show_changes`` is off for deleted entities (the run removed their metadata
    with the row) and ``show_segmentation`` likewise (their stored files are
    gone, so the fetch could only fail).

    Rows are paginated 25 per page: a run can touch thousands of entities, and
    rendering them all at once produces a table tall enough to stall the modal
    (and a refresh payload large enough to drop the websocket).
    """
    if not entities:
        return None
    ui.label(title).classes("text-subtitle1 q-mt-md")
    columns = [
        {
            "name": "title",
            "label": "Title",
            "field": "title",
            "align": "left",
            "sortable": True,
            "style": "max-width: 320px; white-space: normal; word-break: break-word",
        },
    ]
    if show_changes:
        columns.append({"name": "changes", "label": "Changes", "field": "changes", "align": "left", "sortable": True})
    columns.append({"name": "language", "label": "Language", "field": "language", "align": "left", "sortable": True})
    if show_segmentation:
        columns.append(
            {"name": "segmentation", "label": "Segmentation", "field": "segmentation", "align": "center", "sortable": False}
        )
    if include_restored:
        columns.append(
            {
                "name": "restored_shared_id",
                "label": "Restored shared ID",
                "field": "restored_shared_id",
                "align": "left",
                "sortable": False,
            }
        )
    columns.append({"name": "shared_id", "label": "Shared ID", "field": "shared_id", "align": "left", "sortable": True})

    rows = []
    for entity in entities:
        shared_id = entity.shared_id
        writes = writes_by_sid.get(shared_id, [])
        changes = _merge_changes(writes)
        language = _row_language(entity.language, writes)
        row = {
            "shared_id": shared_id,
            "title": titles.get(shared_id) or shared_id,
            "url": _entity_url(shared_id, language, page_instance_url()),
            "language": language or "—",
            "language_raw": language or "en",
            "changes": _change_summary(changes) if show_changes else "—",
            "has_changes": any(c.changed for c in changes),
        }
        if include_restored:
            row["restored_shared_id"] = entity.restored_shared_id or "—"
        rows.append(row)

    table = ui.table(
        rows=rows,
        columns=columns,
        row_key="shared_id",
        pagination={"rowsPerPage": 25},
    ).classes("w-full")
    table.add_slot(
        "body-cell-title",
        """
        <q-td :props="props">
            <span>{{ props.row.title }}</span>
            <a v-if="props.row.url" :href="props.row.url" target="_blank" rel="noopener"
               style="margin-left:8px" title="Open in Uwazi">
                <q-icon name="open_in_new" size="16px" />
            </a>
        </q-td>
        """,
    )
    if show_changes:
        table.add_slot(
            "body-cell-changes",
            """
            <q-td :props="props">
                <span>{{ props.row.changes }}</span>
                <q-btn v-if="props.row.has_changes" dense flat icon="difference" color="primary"
                       title="Show changed properties"
                       @click="$parent.$emit('changes', props.row)" />
            </q-td>
            """,
        )
    if show_segmentation:
        table.add_slot(
            "body-cell-segmentation",
            """
            <q-td :props="props" class="text-no-wrap">
                <q-btn dense flat icon="description" color="grey-8"
                       title="View document segmentation"
                       @click="$parent.$emit('segmentation', props.row)" />
            </q-td>
            """,
        )
    return table


def _rewired_section(rewired: list[Any]) -> None:
    """Render the rewired-relationships touch-set as a table, if non-empty."""
    if not rewired:
        return
    ui.label("Rewired relationships").classes("text-subtitle1 q-mt-md")
    columns = [
        {"name": "entity", "label": "Entity", "field": "entity", "align": "left", "sortable": True},
        {"name": "property", "label": "Property", "field": "property", "align": "left", "sortable": True},
        {"name": "before", "label": "Before", "field": "before", "align": "left", "sortable": False},
    ]
    rows = [{"entity": r.entity.shared_id, "property": r.property_name, "before": _format_value(r.before)} for r in rewired]
    ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")


def _files_section(files: list[Any]) -> None:
    """Render the deleted-files touch-set as a table, if non-empty."""
    if not files:
        return
    ui.label("Deleted files").classes("text-subtitle1 q-mt-md")
    columns = [
        {"name": "shared_id", "label": "Entity", "field": "shared_id", "align": "left", "sortable": True},
        {"name": "originalname", "label": "File", "field": "originalname", "align": "left", "sortable": True},
        {"name": "kind", "label": "Kind", "field": "kind", "align": "left", "sortable": True},
        {"name": "source", "label": "Source", "field": "source", "align": "left", "sortable": True},
    ]
    rows = [{"shared_id": f.shared_id, "originalname": f.originalname, "kind": f.kind, "source": f.source} for f in files]
    ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")


def _changes_dialog(row: dict[str, Any], changes: list[PropertyChange]) -> None:
    """Modal: one entity's property changes (before → after), from run data only.

    Opened from a row's diff button; the rows were computed when the results
    dialog was built, so this is pure rendering — no fetch, no I/O.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").style("min-width: 600px; height: 85dvh"):
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, f"Changes — {row['title']}")
                ui.label(f"sharedId {row['shared_id']}").classes("text-caption text-grey-7")
                columns = [
                    {"name": "property", "label": "Property", "field": "property", "align": "left", "sortable": True},
                    {"name": "before", "label": "Before", "field": "before", "align": "left", "sortable": False},
                    {"name": "after", "label": "After", "field": "after", "align": "left", "sortable": False},
                ]
                rows = [
                    {
                        "property": c.property,
                        "before": c.before,
                        "after": c.after,
                        "changed": c.changed,
                        "cls": "text-weight-medium" if c.changed else "text-grey-7",
                    }
                    for c in changes
                ]
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    table = ui.table(rows=rows, columns=columns, pagination={"rowsPerPage": 0}).classes("w-full")
                    table.add_slot(
                        "body-cell-property",
                        """
                        <q-td :props="props">
                            <span :class="props.row.cls">{{ props.row.property }}</span>
                        </q-td>
                        """,
                    )
                    table.add_slot(
                        "body-cell-after",
                        """
                        <q-td :props="props">
                            <span :class="props.row.cls">{{ props.row.after }}</span>
                        </q-td>
                        """,
                    )
    dialog.open()


def _segmentation_dialog(row: dict[str, Any]) -> None:
    """Modal: one entity's primary-document segmentation, fetched on demand.

    The fetch is the only live Uwazi call this dialog makes (nothing in the run
    data carries document text). A placeholder shows while the background task
    runs; failure degrades to an error label rather than a crash.
    """
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(dialog, f"Document segmentation — {row['title']}")
                body = ui.column().classes("w-full grow").style("overflow-y: auto")
                with body:
                    ui.linear_progress(show_value=False).classes("w-full")
                    ui.label(f"Fetching segmentation for {row['shared_id']} ({row['language_raw']})…").classes(
                        "text-body2 text-grey-7"
                    )
                background_tasks.create(_load_segmentation(body, row), name=f"segmentation {row['shared_id']}")
    dialog.open()


async def _load_segmentation(body: Any, row: dict[str, Any]) -> None:
    """Fetch + render one entity's segmentation into an already-open dialog body."""
    instance = page_instance_url()
    credentials = credentials_for_instance(instance)
    if credentials is None:
        _render_segmentation_error(body, RuntimeError("not logged in to Uwazi"))
        return
    try:
        segmentation = await get_entity_segmentation(
            instance,
            credentials.user,
            credentials.password,
            row["shared_id"],
            row["language_raw"],
        )
    except Exception as exc:  # noqa: BLE001 — degrade to an error label, never crash the UI
        _render_segmentation_error(body, exc)
        return
    body.clear()
    with body:
        page_count = max((p.page_number for p in segmentation.paragraphs), default=0)
        ui.label(
            f"{segmentation.filename} — status={segmentation.status}, "
            f"pages={page_count}, paragraphs={len(segmentation.paragraphs)}"
        ).classes("text-subtitle1 text-grey-7")
        ordered = sorted(segmentation.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
        text = "\n".join(f"[page {p.page_number}] {p.text}" for p in ordered) or "(no paragraphs)"
        ui.code(text).classes("w-full").style("white-space: pre-wrap")


def _render_segmentation_error(body: Any, exc: Exception) -> None:
    """Replace the dialog body with a readable failure (the tool's degrade-to-string spirit)."""
    body.clear()
    with body:
        ui.label(f"{type(exc).__name__}: {exc}").classes("text-body2 text-red-10").style("white-space: pre-wrap")


def _delete_dialog(run_id: str) -> None:
    """Yes/no confirmation before permanently removing a run."""
    instance = page_instance_url()
    if _session_ended(instance):
        return
    _confirm_dialog(
        "Delete run",
        f"Permanently remove run {run_id!r}? This deletes the entire run folder. "
        "If the run is EXECUTED, revert will no longer be possible.",
        lambda: delete_run(instance, run_id),
        success_msg=f"Deleted {run_id}",
        instance=instance,
        run_id=run_id,
        kind="deleting",
    )


def _rename_confirm(dialog: Any, old_id: str, name_input: Any) -> None:
    """Validate the new name, then rename the run via a background task."""
    new_id = (name_input.value or "").strip()
    if not new_id:
        ui.notify("New name is required", type="warning")
        return
    if new_id == old_id:
        dialog.close()
        return
    instance = page_instance_url()
    if _session_ended(instance):
        return
    existing = {r.run_id for r in list_runs(instance)}
    if new_id in existing:
        ui.notify(f"A task named {new_id!r} already exists", type="warning")
        return
    # The claim belongs to the run being renamed, not to the new name: another
    # operator could otherwise operate on this run while it is moving.
    if not operation_gate.try_claim_run(instance, old_id, "renaming"):
        ui.notify(_busy_run_text(instance, old_id), type="warning")
        return
    dialog.close()

    async def _do() -> None:
        rename_run(instance, old_id, new_id)

    background_tasks.create(
        _run_async(_do(), f"Renamed {old_id} → {new_id}", instance, old_id, "renaming"),
        name=f"rename {old_id}",
    )


def _confirm_and_close(
    dialog: Any,
    on_confirm: Any,
    success_msg: str,
    instance: str,
    run_id: str | None = None,
    kind: str = "deleting",
) -> None:
    dialog.close()
    # Let the outbox flush the close before the background task starts: the
    # revert/execute run a synchronous Uwazi login first, and blocking the
    # event loop before the flush stalls the modal visibly open.
    ui.timer(0.05, lambda: _start_confirmed_task(on_confirm, success_msg, instance, run_id, kind), once=True)


def _start_confirmed_task(
    on_confirm: Any,
    success_msg: str,
    instance: str,
    run_id: str | None = None,
    kind: str = "deleting",
) -> None:
    # Claim at the actual launch — a failed claim aborts the whole confirm
    # action with the holder's reason and no task. Only the SAME run can
    # conflict; other runs and other instances stay free.
    if run_id is not None and not operation_gate.try_claim_run(instance, run_id, kind):
        ui.notify(_busy_run_text(instance, run_id), type="warning", multi_line=True)
        return
    try:
        result = on_confirm()
    except Exception:
        # A sync op (delete) that raises before its task exists must still
        # release, or the claim would block that run until restart.
        if run_id is not None:
            operation_gate.release_run(instance, run_id, kind)
        raise
    if result is _ABORT:
        return
    if hasattr(result, "__await__"):
        background_tasks.create(
            _run_async(result, success_msg, instance, run_id, kind),
            name=success_msg,
        )
    else:
        ui.notify(success_msg, type="positive")
        if run_id is not None:
            operation_gate.release_run(instance, run_id, kind)
        _broadcast_rows(instance)
