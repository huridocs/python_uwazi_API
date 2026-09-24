"""NiceGUI web UI for the Uwazi Property Filler.

Single page: a login gate, a 3-pane layout (pending/validated lists | PDF or
Markdown viewer | property form), extension management + logs modals. Mirrors
``uwazi_admin_agent/drivers/web/app.py``'s header/login/background-task/log-buffer
patterns. This is a driver: it wires :mod:`service` to the UI and holds no
business logic.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Response
from loguru import logger
from nicegui import app, background_tasks, context, ui

from uwazi_property_filler.configuration import (
    APP_PORT,
    FILTER_PROPERTY,
    PROPERTY_NAME,
    PROPERTY_TEMPLATE,
    TEMPLATE_NAME,
)
from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_kind import ExtensionKind
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.metadata_text import relationship_entries
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.drivers.web import service as svc

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"

# In-memory ring buffer of recent log lines (mirrors container stderr output).
_LOG_BUFFER: deque[str] = deque(maxlen=2000)

_CONTROLLED_UWAZI_URL = os.environ.get("UWAZI_URL", "not configured")

# Fillable property types (skip preview/nested/generatedid/image/media + title).
_SKIP_TYPES = {"preview", "nested", "generatedid", "image", "media"}
_MAX_LIST_ROWS = 200

# Left-pane UI tweaks. When the filter dropdown is open, its popup menu items
# wrap so the full label + document count stay readable; once an option is
# picked, Quasar's closed-select label ellipsizes to the pane width. The tabs
# are compacted (smaller font, tighter padding) to fit small screens, and
# document rows get tighter padding so titles + subtitles stay compact.
_LEFT_PANE_CSS = """
.pf-filter-menu .q-item__label {
    white-space: normal;
    word-break: break-word;
}
.pf-compact-tabs .q-tab {
    min-height: 28px;
    padding: 0 8px;
}
.pf-compact-tabs .q-tab__label {
    font-size: 0.7rem;
}
.pf-compact-tabs .q-tab__icon {
    font-size: 0.9rem;
}
.pf-compact-tabs .q-tabs__content {
    min-height: 28px;
}
.pf-doc-list .q-item {
    padding: 0;
    min-height: 0;
    border-bottom: 1px solid rgba(0, 0, 0, 0.12);
}
.pf-doc-list .q-item:last-child {
    border-bottom: none;
}
.pf-doc-list .q-item__section--side {
    padding: 0 0 0 2px;
    min-width: 0;
}
.pf-doc-title {
    font-size: 0.75rem;
    line-height: 1.2;
}
.pf-doc-subtitle {
    font-size: 0.65rem;
    line-height: 1.2;
    color: #9e9e9e;
}
.pf-doc-list .pf-doc-row-selected {
    background-color: rgba(44, 62, 80, 0.12);
    box-shadow: inset 3px 0 0 #18bc9c;
}
.pf-doc-list .pf-doc-row-selected .pf-doc-title {
    color: #2c3e50;
    font-weight: 600;
}
"""

# Main 3-pane layout. The outer splitter keeps the left list at 20% and the
# inner splitter splits the remaining 80% into 75/25, so the PDF viewer gets
# 60% of the page width and the form gets 20%. Vertically the layout becomes a
# bounded app shell that fills everything below the fixed header: Quasar's
# ``q-page`` already carries ``min-height: calc(100vh - <measured header>)``,
# so collapsing it to ``height: 0`` turns that min-height into the content
# height without guessing the header size. Each pane then scrolls internally
# (the list and the form scroll; the viewer iframe fills its pane), so tall
# content never pushes the layout past the viewport.
# Quasar gives the splitter panels ``height: 100%``, which resolves to ``auto``
# under this flex parent and blocks stretching, so it is overridden to ``auto``.
_LAYOUT_CSS = """
.q-page:has(.pf-layout) {
    display: flex;
    flex-direction: column;
    height: 0;
    overflow: hidden;
}
.nicegui-content:has(.pf-layout) {
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    padding: 0;
}
.pf-layout {
    flex: 1 1 auto;
    width: 100%;
    min-height: 0;
}
.pf-layout .q-splitter__panel {
    display: flex;
    flex-direction: column;
    height: auto;
    min-height: 0;
}
/* List and form panes grow with their content and scroll inside the panel. */
.pf-layout .q-splitter__panel > .nicegui-column:not(.pf-viewer) {
    flex: 1 0 auto;
}
/* The nested viewer splitter and the viewer column fill and shrink to fit. */
.pf-layout .q-splitter__panel > .nicegui-splitter,
.pf-layout .q-splitter__panel > .pf-viewer {
    flex: 1 1 auto;
    min-height: 0;
}
.pf-viewer {
    overflow: hidden;
}
.pf-viewer-iframe {
    flex: 1 1 auto;
    min-height: 0;
}
"""


def _is_logged_in() -> bool:
    return bool(app.storage.user.get("user") and app.storage.user.get("password"))


def _current_service() -> svc.PropertyFillerService | None:
    return svc.get_service()


def _log_sink(message: Any) -> None:
    _LOG_BUFFER.append(str(message).rstrip("\n"))


logger.add(_log_sink, level="DEBUG", format="{time:HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}")


def _notify(message: str, type: str = "positive", **kwargs: Any) -> None:  # noqa: A002
    """Notify connected clients; safe from background tasks.

    ``ui.notify`` requires a client context, which background tasks lack. This
    mirrors ``uwazi_admin_agent``'s ``_broadcast_notify``: iterate all
    connected clients and enqueue a notify message to each.
    """
    options = {"message": str(message), "type": type, **kwargs}
    for client in app.clients():
        client.outbox.enqueue_message("notify", options, client.id)


def _start_ongoing(client: Any, key: str, message: str) -> None:
    """Create a dismissible ongoing notification on ``client``.

    Quasar's ``ongoing`` type never auto-dismisses (``timeout: 0``), so the
    element must be dismissed later via :func:`_finish_ongoing`. The client is
    an explicit argument because NiceGUI keeps one slot stack per asyncio task:
    a background task (Connect & Refresh runs as one) starts with an empty
    stack, so ``context.client`` raises there and the toast would silently
    never appear. Handles are keyed per client so two open tabs never dismiss
    each other's toasts.
    """
    if client is None or client.is_deleted:
        return
    with client.layout:
        _ongoing_notifications[(client.id, key)] = ui.notification(
            message,
            type="ongoing",
            spinner=True,
            timeout=None,
        )


def _finish_ongoing(client: Any, key: str) -> None:
    """Dismiss the ongoing notification for ``(client, key)`` and drop it."""
    notification = _ongoing_notifications.pop((client.id, key), None) if client is not None else None
    if notification is not None and _is_attached(notification):
        notification.dismiss()


_ongoing_notifications: dict[tuple[int, str], Any] = {}


def _is_attached(element: Any) -> bool:
    """Whether ``element`` still belongs to a live NiceGUI client.

    Background tasks routinely outlive the tab that started them (page
    reload, logout, ``reconnect_timeout``). Mutating an element whose
    client has been deleted raises ``RuntimeError: The client this element
    belongs to has been deleted.``, so callers should bail out first.
    """
    try:
        client = element.client
    except RuntimeError:
        return False
    return not client.is_deleted


def _login_page() -> None:
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    with ui.column().classes("w-full items-center justify-center min-h-screen"):
        with ui.card().classes("w-full max-w-sm"):
            ui.label("Uwazi Property Filler").classes("text-h6")
            ui.label("Log in with a Uwazi account. The page stays locked until the credentials are validated.").classes(
                "text-body1 q-mt-sm"
            )
            user_input = ui.input("Username").classes("w-full text-h6")
            password_input = ui.input("Password", password=True).classes("w-full text-h6")
            error_label = ui.label().classes("text-negative text-body2")

            async def _submit() -> None:
                # Empty fields default to the local admin account (admin/admin).
                user = (user_input.value or "").strip() or "admin"
                password = password_input.value or "admin"
                try:
                    service = svc.build_service(user, password)
                    await service.login(user, password)
                    await service.build_runners()
                except Exception as exc:  # noqa: BLE001 — surface every validation failure
                    error_label.set_text(f"Login failed: {exc}")
                    return
                svc.set_service(service)
                app.storage.user["user"] = user
                app.storage.user["password"] = password
                ui.navigate.to("/app")

            password_input.on("keydown.enter", _submit)
            with ui.row().classes("q-mt-lg"):
                ui.button("Log in", color="primary", on_click=_submit)


def _logout() -> None:
    app.storage.user.pop("user", None)
    app.storage.user.pop("password", None)
    svc.set_service(None)
    ui.notify("Logged out", type="positive")
    ui.navigate.to("/")


def _logs_dialog() -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full"):
            dialog.props("maximized")
            with ui.row().classes("items-center justify-between q-mb-md"):
                ui.label("Service Logs").classes("text-h6")
                with ui.row():
                    ui.button("Refresh", icon="refresh", on_click=lambda: _refresh_logs(log_area)).props("flat dense")
                    ui.button("Close", icon="close", on_click=lambda: dialog.close()).props("flat dense")
            log_area = ui.textarea().classes("w-full").props("readonly autogrow").classes("font-mono text-caption")
            log_area.style("height: calc(100vh - 140px)")

            def _refresh_logs(la: Any) -> None:
                la.value = "\n".join(_LOG_BUFFER)

            _refresh_logs(log_area)
            ui.timer(1.0, lambda: _refresh_logs(log_area))
        dialog.open()


def _extensions_dialog() -> None:
    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
            with ui.row().classes("items-center justify-between q-mb-md w-full"):
                ui.label("Extensions").classes("text-h6")
                ui.button("Close", icon="close", on_click=lambda: dialog.close()).props("flat dense")
            extensions_container = ui.column().classes("w-full")

            def _render_rows() -> None:
                if not _is_attached(extensions_container):
                    return
                extensions_container.clear()
                service = _current_service()
                if service is None:
                    return

                async def _load() -> None:
                    records = await service.list_extensions()
                    if not _is_attached(extensions_container):
                        return
                    for record in records:
                        with extensions_container:
                            with ui.row().classes("items-center w-full justify-between"):
                                kind_cat = f"{record.kind.value}/{record.category.value}"
                                ui.label(f"{record.name} ({kind_cat})").classes("q-mr-md")
                                switch = ui.switch(value=record.enabled)
                                switch.on_value_change(
                                    lambda e, rid=record.id: background_tasks.create(_toggle(rid, e.value))
                                )
                                stats_label = ui.label("")

                                async def _stats(rid: str = record.id) -> None:
                                    stats = await service.extension_stats(rid)
                                    if not _is_attached(stats_label):
                                        return
                                    stats_label.set_text(f"processed {stats['processed']} / errors {stats['errors']}")

                                background_tasks.create(_stats())

                background_tasks.create(_load())

            async def _toggle(extension_id: str, enabled: bool) -> None:
                service = _current_service()
                if service is None:
                    return
                await service.set_extension_enabled(extension_id, enabled)
                _render_rows()

            _render_rows()
            ui.separator().classes("w-full q-my-md")

            ui.label("Register extension").classes("text-subtitle1")
            reg_id = ui.input("ID").classes("w-full")
            reg_name = ui.input("Name").classes("w-full")
            reg_kind = ui.select(["local", "remote"], value="local", label="Kind").classes("w-full")
            reg_category = ui.select([c.value for c in ExtensionCategory], value="suggestion", label="Category").classes(
                "w-full"
            )
            reg_endpoint = ui.input("Endpoint (remote)").classes("w-full")
            reg_entrypoint = ui.input("Entrypoint (local)").classes("w-full")
            reg_config = ui.input("Config JSON").classes("w-full")
            reg_error = ui.label().classes("text-negative text-body2")

            async def _register() -> None:
                service = _current_service()
                if service is None:
                    return
                try:
                    record = ExtensionRecord(
                        id=(reg_id.value or "").strip(),
                        name=(reg_name.value or "").strip(),
                        kind=ExtensionKind(reg_kind.value),
                        category=ExtensionCategory(reg_category.value),
                        endpoint=reg_endpoint.value or None,
                        entrypoint=reg_entrypoint.value or None,
                        config=json.loads(reg_config.value) if reg_config.value else {},
                    )
                    await service.register_extension(record)
                    if _is_attached(reg_error):
                        reg_error.set_text("")
                    _render_rows()
                except Exception as exc:  # noqa: BLE001
                    if _is_attached(reg_error):
                        reg_error.set_text(f"Register failed: {exc}")

            ui.button("Register", icon="add", on_click=lambda: background_tasks.create(_register())).props("color=primary")
        dialog.open()


def _build_page() -> None:
    service = _current_service()
    if service is None:
        ui.navigate.to("/")
        return

    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.label("Uwazi Property Filler").classes("text-h6 q-mr-md")
        with ui.row().classes("items-center"):
            ui.icon("link", color="secondary").classes("q-mr-xs")
            ui.link(_CONTROLLED_UWAZI_URL, _CONTROLLED_UWAZI_URL, new_tab=True).classes("text-white")
            ui.button(
                "Connect & Refresh",
                icon="refresh",
                on_click=lambda: background_tasks.create(_refresh()),
            ).props("flat color=secondary")
            with ui.button(icon="menu").props("flat round color=secondary"):
                with ui.menu():
                    ui.menu_item("Extensions", _extensions_dialog)
                    ui.menu_item("Logs", _logs_dialog)
                    ui.separator()
                    ui.menu_item("Log out", _logout)

    # Per-client UI state.
    state: dict[str, Any] = {
        "service": service,
        "template": TEMPLATE_NAME,
        "language": "en",
        "filter_value": None,
        "search_text": "",
        "pending": [],
        "validated": [],
        "selected": None,
        "properties": [],
        "metadata": {},
        "widgets": {},
        "raw_values": {},
    }
    # Captured at build time: background tasks have no slot stack of their
    # own, so ``context.client`` is unavailable inside them.
    page_client = context.client
    page_client._pf_state = state  # noqa: SLF001

    # --- 3-pane layout: left list | middle viewer | right form ------------
    # Outer splitter: left 20%. Inner splitter takes the remaining 80% and,
    # at value=75, gives the viewer 60% and the form 20% of the full width.
    ui.add_css(_LEFT_PANE_CSS)
    ui.add_css(_LAYOUT_CSS)
    with ui.splitter(value=20).classes("w-full pf-layout") as outer:
        with outer.before, ui.column().classes("q-pa-sm"):
            filter_select = (
                ui.select({}, label="Filter", with_input=False)
                .props("popup-content-class=pf-filter-menu")
                .classes("w-full pf-filter-select")
            )
            state["filter_select"] = filter_select

            async def _load_filters() -> None:
                options = await service.get_filter_options(state["template"], state["language"])
                if not _is_attached(filter_select):
                    return
                filter_select.set_options({"ALL": "ALL", **options}, value="ALL")

            background_tasks.create(_load_filters())

            def _on_filter_change(e: Any) -> None:
                value = e.value
                state["filter_value"] = None if value in (None, "ALL") else value
                if value in (None, "ALL"):
                    label = "all documents"
                else:
                    label = f'{FILTER_PROPERTY} = "{value}"'
                _start_ongoing(page_client, "filter", f"Showing cached documents for {label}…")
                background_tasks.create(_load_lists(select_first=True))

            filter_select.on_value_change(_on_filter_change)

            search_input = ui.input("Search", placeholder="Filter by title or subtitle").classes("w-full q-mt-xs")

            def _apply_search() -> None:
                state["search_text"] = (search_input.value or "").strip().lower()
                _render_lists()

            search_input.on("keydown.enter", _apply_search)
            ui.button("Search", icon="search", on_click=_apply_search).props("flat dense color=secondary")

            with ui.tabs().classes("w-full q-mt-sm pf-compact-tabs") as tabs:
                pending_tab = ui.tab("pending", "Pending (0)")
                validated_tab = ui.tab("validated", "Validated (0)")
            with ui.tab_panels(tabs, value="pending").classes("w-full"):
                with ui.tab_panel("pending"):
                    pending_caption = ui.label("").classes("text-caption")
                    pending_list = ui.list().classes("w-full pf-doc-list")
                with ui.tab_panel("validated"):
                    validated_caption = ui.label("").classes("text-caption")
                    validated_list = ui.list().classes("w-full pf-doc-list")

            state["tabs"] = tabs
            state["pending_tab"] = pending_tab
            state["validated_tab"] = validated_tab
            state["pending_caption"] = pending_caption
            state["validated_caption"] = validated_caption

            async def _load_initial() -> None:
                await _load_lists(select_first=True)

            background_tasks.create(_load_initial())

        with outer.after:
            with ui.splitter(value=75).classes("w-full") as inner:
                with inner.before:
                    viewer_container = ui.column().classes("w-full pf-viewer")
                    state["viewer_container"] = viewer_container
                with inner.after:
                    form_container = ui.column().classes("w-full q-pa-sm")
                    state["form_container"] = form_container

    # --- list loading ------------------------------------------------------
    async def _load_lists(select_first: bool = False) -> None:
        template = state["template"]
        if not template:
            return
        try:
            pending, validated = await service.list_pdfs(template, state["language"], state["filter_value"])
            state["pending"] = pending
            state["validated"] = validated
            _render_lists()
            if select_first:
                await _select_first_visible()
        finally:
            _finish_ongoing(page_client, "filter")

    def _first_visible() -> PdfItem | None:
        """First row the left pane would show, search text included.

        Pending rows come before validated ones, mirroring the default tab
        order; the tab is switched when the winner lives on the other one so
        the highlighted row is always the visible one.
        """
        for tab_key, bucket in (("pending", state["pending"]), ("validated", state["validated"])):
            for item in bucket[:_MAX_LIST_ROWS]:
                if _matches_search(item):
                    # Keep the winner visible: it may live on the other tab.
                    state["tabs"].set_value(tab_key)
                    return item
        return None

    async def _select_first_visible() -> None:
        item = _first_visible()
        if item is None:
            return
        if state["selected"] is not None and item.shared_id == state["selected"].shared_id:
            return
        await _select_pdf(item)

    def _matches_search(item: PdfItem) -> bool:
        needle = state.get("search_text", "")
        if not needle:
            return True
        haystack = f"{item.title} {item.subtitle}".lower()
        return needle in haystack

    def _render_lists() -> None:
        if not _is_attached(pending_list) or not _is_attached(validated_list):
            return
        pending = [it for it in state["pending"] if _matches_search(it)]
        validated = [it for it in state["validated"] if _matches_search(it)]
        pending_list.clear()
        validated_list.clear()
        with pending_list:
            for item in pending[:_MAX_LIST_ROWS]:
                _pdf_row(item)
        with validated_list:
            for item in validated[:_MAX_LIST_ROWS]:
                _pdf_row(item)
        state["pending_tab"].set_label(f"Pending ({len(pending)})")
        state["validated_tab"].set_label(f"Validated ({len(validated)})")
        _update_row_hint(state["pending_caption"], len(pending))
        _update_row_hint(state["validated_caption"], len(validated))

    def _update_row_hint(caption: Any, total: int) -> None:
        caption.set_text(f"showing first {_MAX_LIST_ROWS} of {total}" if total > _MAX_LIST_ROWS else "")

    def _pdf_row(item: PdfItem) -> None:
        label = item.title or item.shared_id
        selected = state["selected"]
        is_selected = selected is not None and item.shared_id == selected.shared_id
        classes = "cursor-pointer pf-doc-row-selected" if is_selected else "cursor-pointer"
        with ui.item(on_click=lambda it=item: background_tasks.create(_select_pdf(it))).classes(classes):
            with ui.item_section():
                ui.item_label(label).classes("pf-doc-title")
                if item.subtitle:
                    ui.item_label(item.subtitle).classes("pf-doc-subtitle")

    async def _reload_after_refresh() -> None:
        """Re-render lists and filter counts right after the data refresh.

        Each step is guarded so a failure in one (e.g. filter options hitting
        Uwazi) cannot hide the freshly fetched results in the other.
        """
        for step in (_load_lists, _load_filters):
            try:
                await step()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Connect & Refresh: {} failed: {}", step.__name__, exc)

    async def _refresh() -> None:
        template = state["template"]
        if not template:
            _notify("Select a template first", type="warning")
            return
        _start_ongoing(page_client, "refresh", "Fetching documents from the instance…")
        try:
            count = await service.refresh(template, state["language"])
        except Exception as exc:  # noqa: BLE001
            _notify(f"Refresh failed: {exc}", type="negative")
            return
        finally:
            _finish_ongoing(page_client, "refresh")
        # Render the new lists immediately: the PDF byte caching below can
        # take minutes, and before this it delayed the UI update with no
        # visible feedback at all.
        await _reload_after_refresh()
        _notify(f"Refreshed: cached {count} documents", type="positive")
        _start_ongoing(page_client, "pdfs", "Caching PDF files…")
        try:
            cached = await service.warm_pdf_cache(template, state["language"])
        except Exception as exc:  # noqa: BLE001
            _notify(f"PDF caching failed: {exc}", type="negative")
            return
        finally:
            _finish_ongoing(page_client, "pdfs")
        _notify(f"Cached {cached} PDF files", type="positive")

    async def _select_pdf(item: PdfItem) -> None:
        state["selected"] = item
        # Re-render so the left-list highlight follows the selection at once.
        _render_lists()
        await _load_form()
        await _load_middle()

    async def _load_middle() -> None:
        item = state["selected"]
        if item is None or not _is_attached(viewer_container):
            return
        viewer_container.clear()
        with viewer_container:
            ui.element("iframe").props(f'src="/viewer/{item.filename}" frameborder="0"').classes("w-full pf-viewer-iframe")

    # --- right form --------------------------------------------------------
    def _section(title: str) -> Any:
        """One bordered section stacked in the right pane: title, rule, content."""
        card = ui.card().classes("w-full q-pa-sm q-mb-sm").props("flat bordered")
        with card:
            ui.label(title).classes("text-subtitle2 text-weight-bold")
            ui.separator().classes("w-full q-my-xs")
        return card

    def _pick_value(value: str) -> None:
        """Apply a value chosen in the "All values" list to the current value."""
        if not state["properties"]:
            return
        prop = state["properties"][0]
        widget = state["widgets"].get(prop.name)
        if prop.type.value in ("select", "multiselect", "relationship"):
            # Select-like fields render read-only labels fed from raw_values.
            state["raw_values"][prop.name] = value
            if widget is not None:
                widget.set_text(_display_value(value))
        else:
            # Editable widgets are read back through their own .value.
            state["raw_values"].pop(prop.name, None)
            if widget is not None:
                widget.value = value
        # Persist the picked value to Uwazi right away, like the checkboxes.
        save = state.get("schedule_save")
        if save is not None:
            save()

    async def _load_form() -> None:
        item = state["selected"]
        if item is None or not _is_attached(form_container):
            return
        form_container.clear()
        properties = await service.get_template_properties(state["template"])
        # Only the configured fill property is rendered (``.env`` PROPERTY_FILLER_PROPERTY).
        state["properties"] = [
            p for p in properties if p.type.value not in _SKIP_TYPES and p.name != "title" and p.name == PROPERTY_NAME
        ]
        metadata = await service.get_entity_metadata(item.shared_id, state["template"], state["language"])
        state["metadata"] = metadata
        state["widgets"] = {}
        state["raw_values"] = {}

        # Possible values of the fill property come from one of two places: a
        # template of entities (``PROPERTY_FILLER_PROPERTY_TEMPLATE``, e.g.
        # TOPIC — with the related template configured on a relationship
        # property as fallback) or, for select-like properties, the property's
        # thesaurus labels.
        fill_property = state["properties"][0] if state["properties"] else None
        values_template = PROPERTY_TEMPLATE
        if not values_template and fill_property is not None and fill_property.type.value == "relationship":
            related_template = getattr(fill_property, "content", None)
            if related_template:
                values_template = await service.template_name_for_id(related_template) or ""
        template_values: list[dict[str, str]] = []
        thesaurus_labels: list[str] = []
        if values_template:
            try:
                template_values = await service.search_entities(values_template, state["language"], None)
            except Exception as exc:  # noqa: BLE001 — the form must render regardless
                logger.warning("Could not load values from template '{}': {}", values_template, exc)
        elif fill_property is not None and fill_property.type.value in ("select", "multiselect"):
            thesaurus_id = getattr(fill_property, "content", None)
            if thesaurus_id:
                thesaurus_labels = await service.get_thesaurus_labels(thesaurus_id, state["language"])
        # Entities already linked to this PDF stay selected even when the
        # template search does not return them.
        checked: dict[str, str] = {}
        if values_template and fill_property is not None:
            for entry in relationship_entries(metadata.get(fill_property.name)):
                checked[entry["shared_id"]] = entry["title"]
        if not _is_attached(form_container):
            return

        # Elements rebuilt by the selection handlers (created while rendering).
        current_box: Any = None
        values_box: Any = None
        filter_input: Any = None

        def _write_selection() -> None:
            """Persist the checked template entities for validation."""
            if fill_property is None:
                return
            selected = [{"shared_id": shared_id, "title": title} for shared_id, title in checked.items()]
            state["raw_values"][fill_property.name] = selected
            widget = state["widgets"].get(fill_property.name)
            if widget is not None:
                widget.set_text(_display_value(selected))

        # Live persistence: every checkbox mark/unmark is written to Uwazi
        # right away, and the checkboxes then adopt whatever the instance
        # returns (its normalization, or values changed elsewhere meanwhile).
        save_lock = asyncio.Lock()
        save_gen = 0

        def _current_fill_value() -> Any:
            """The value that would be saved for the fill property right now."""
            if fill_property is None:
                return None
            if fill_property.name in state["raw_values"]:
                return state["raw_values"][fill_property.name]
            widget = state["widgets"].get(fill_property.name)
            return widget.value if widget is not None else None

        def _schedule_save() -> None:
            nonlocal save_gen
            if fill_property is None:
                return
            item = state["selected"]
            if item is None:
                return
            save_gen += 1
            # Snapshot the document and value now: a later document switch
            # rebuilds the form, and the write must carry the toggled value,
            # not whatever the form holds by the time the lock is acquired.
            payload = {fill_property.name: _current_fill_value()}
            background_tasks.create(_persist_selection(save_gen, item, payload))

        state["schedule_save"] = _schedule_save

        async def _persist_selection(gen: int, item: PdfItem, payload: dict[str, Any]) -> None:
            """Write the toggled selection to Uwazi, then re-read it from there.

            Serialized by ``save_lock`` so rapid toggles cannot interleave
            Uwazi's read-modify-write update. When a newer toggle was
            scheduled meanwhile, this cycle skips both the write and the
            re-read (the newer cycle persists the fresher state), so a burst
            of toggles costs exactly one Uwazi write.
            """
            async with save_lock:
                if gen != save_gen:
                    return  # a newer toggle supersedes this cycle
                try:
                    await service.save_entity_metadata(
                        item.shared_id,
                        state["template"],
                        state["language"],
                        payload,
                    )
                except Exception as exc:  # noqa: BLE001 — the toggle stays applied locally
                    _notify(f"Could not save to Uwazi: {exc}", type="negative")
                    return
                if gen != save_gen:
                    return  # the newer cycle owns the write and the re-read
                try:
                    metadata = await service.get_entity_metadata(item.shared_id, state["template"], state["language"])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not re-read values from Uwazi for {}: {}", item.shared_id, exc)
                    return
                if gen != save_gen or state["selected"] is not item or not _is_attached(form_container):
                    return
                # Adopt the instance's values so the checkboxes show what
                # Uwazi actually stores, even if it changed meanwhile.
                state["metadata"] = metadata
                _sync_from_instance(metadata)

        def _sync_from_instance(metadata: dict[str, Any]) -> None:
            """Re-render the fill-property UI from the instance's metadata."""
            if fill_property is None:
                return
            fresh = metadata.get(fill_property.name)
            if values_template:
                checked.clear()
                for entry in relationship_entries(fresh):
                    checked[entry["shared_id"]] = entry["title"]
                _write_selection()
                _render_current()
                if values_box is not None:
                    _render_values(filter_input.value if filter_input is not None else "")
            else:
                # Select-like field: refresh the read-only label from Uwazi.
                state["raw_values"][fill_property.name] = fresh
                widget = state["widgets"].get(fill_property.name)
                if widget is not None:
                    widget.set_text(_display_value(fresh))

        def _render_current() -> None:
            """Current values: checked boxes; unchecking removes the value."""
            if current_box is None or not _is_attached(current_box):
                return
            current_box.clear()
            with current_box:
                if not checked:
                    ui.label("No value selected.").classes("text-caption text-grey-6")
                for shared_id, title in list(checked.items()):
                    entry = {"shared_id": shared_id, "title": title}
                    box = ui.checkbox(title, value=True).classes("w-full")
                    box.on_value_change(lambda e, chosen=entry: _toggle_value(chosen, bool(e.value)))

        def _pool() -> list[dict[str, str]]:
            """Template entities plus checked ones missing from the search."""
            entries = list(template_values)
            seen = {entry["shared_id"] for entry in entries}
            entries.extend(
                {"shared_id": shared_id, "title": title} for shared_id, title in checked.items() if shared_id not in seen
            )
            return entries

        def _render_values(needle: str = "") -> None:
            """Section 3 list: template checkboxes (or thesaurus labels)."""
            if values_box is None or not _is_attached(values_box):
                return
            needle = (needle or "").strip().lower()
            pool = _pool() if values_template else []
            shown = [entry for entry in pool if needle in entry["title"].lower()]
            shown_labels = [label for label in thesaurus_labels if needle in label.lower()]
            values_box.clear()
            with values_box:
                if values_template:
                    if not pool:
                        ui.label(f"No entities found in template '{values_template}'.").classes("text-caption text-grey-6")
                    elif not shown:
                        ui.label("No matching value.").classes("text-caption text-grey-6")
                    for entry in shown:
                        box = ui.checkbox(entry["title"], value=entry["shared_id"] in checked).classes("w-full")
                        box.on_value_change(lambda e, chosen=entry: _toggle_value(chosen, bool(e.value)))
                else:
                    if not thesaurus_labels:
                        ui.label("This property has no predefined values.").classes("text-caption text-grey-6")
                    elif not shown_labels:
                        ui.label("No matching value.").classes("text-caption text-grey-6")
                    for label in shown_labels:
                        with ui.item(on_click=lambda chosen=label: _pick_value(chosen)).classes("cursor-pointer"):
                            with ui.item_section():
                                ui.item_label(label).classes("pf-doc-title")

        def _toggle_value(entry: dict[str, str], is_on: bool) -> None:
            """Add/remove an entity, refresh both checkbox lists, then persist."""
            if is_on:
                checked[entry["shared_id"]] = entry["title"]
            else:
                checked.pop(entry["shared_id"], None)
            _write_selection()
            _render_current()
            if values_box is not None:
                _render_values(filter_input.value if filter_input is not None else "")
            _schedule_save()

        if values_template:
            # Section 1 renders checkboxes instead of an editable widget, so
            # seed raw_values with the current selection for validation.
            _write_selection()

        with form_container:
            # 0 — which document this pane is labeling.
            with _section("Document"):
                ui.label(item.title or item.shared_id).classes("text-subtitle1 text-weight-medium")
                if item.subtitle:
                    ui.label(item.subtitle).classes("text-caption text-grey-7")

            # 1 — current PROPERTY_FILLER_PROPERTY value of the selected PDF:
            # every value shown as a checked box; unchecking removes it.
            with _section("Current value"):
                if not state["properties"]:
                    ui.label(f"Property '{PROPERTY_NAME}' not found in the template.").classes("text-caption text-grey-6")
                elif values_template:
                    for prop in state["properties"]:
                        current_box = ui.column().classes("w-full")
                        _render_current()
                else:
                    for prop in state["properties"]:
                        _render_property_field(prop, metadata)

            # Validation runs right after the current values, spanning the
            # column from right to left.
            ui.button(
                "Mark as validated",
                icon="task_alt",
                on_click=lambda: background_tasks.create(_validate()),
            ).props("color=primary").classes("w-full q-mb-sm")

            # 2 — suggestions stay empty: the suggestion flow is not
            # implemented yet, so nothing is fetched or rendered here.
            with _section("Suggestions"):
                ui.label("Not implemented yet.").classes("text-caption text-grey-6")

            # 3 — every possible value: the filter sits above the list
            # because the list can get long. Template-backed values carry a
            # checkbox that adds/removes them from the PDF; thesaurus labels
            # are click-to-set.
            with _section("All values"):
                option_count = len(template_values) if values_template else len(thesaurus_labels)
                filter_input = ui.input(placeholder=f"Filter {option_count} values…").classes("w-full")
                filter_input.on_value_change(lambda e: _render_values(e.value or ""))
                values_box = ui.column().classes("w-full").style("max-height: 40vh; overflow-y: auto")
                _render_values()

    def _render_property_field(prop: Any, metadata: dict[str, Any]) -> None:
        prop_type = prop.type.value
        value = metadata.get(prop.name)
        label = prop.label or prop.name
        widgets = state["widgets"]

        if prop_type in ("select", "multiselect", "relationship"):
            state["raw_values"][prop.name] = value
            ui.label(label).classes("text-caption q-mb-xs")
            w = ui.label(_display_value(value)).classes("text-body1")
        elif prop_type in ("text", "markdown"):
            w = ui.textarea(label, value=_scalar_text(value)).classes("w-full")
        elif prop_type == "numeric":
            w = ui.number(label, value=_number(value)).classes("w-full")
        elif prop_type == "date":
            w = ui.input(label, value=_date_text(value)).classes("w-full")
        elif prop_type == "daterange":
            w = ui.input(label, value=_daterange_text(value)).classes("w-full")
        elif prop_type == "link":
            d = _link_dict(value)
            w = ui.input(label, value=d.get("label", "")).classes("w-full")
        elif prop_type == "geolocation":
            lat, lon = _geo_pair(value)
            with ui.row().classes("w-full"):
                w = ui.number(f"{label} lat", value=lat).classes("w-full")
                widgets[f"{prop.name}__lon"] = ui.number(f"{label} lon", value=lon).classes("w-full")
        else:
            w = ui.input(label, value=_scalar_text(value)).classes("w-full")

        widgets[prop.name] = w

    async def _validate() -> None:
        item = state["selected"]
        if item is None:
            return
        service = _current_service()
        if service is None:
            return
        metadata: dict[str, Any] = {}
        for prop in state["properties"]:
            if prop.name in state["raw_values"]:
                metadata[prop.name] = state["raw_values"][prop.name]
                continue
            w = state["widgets"].get(prop.name)
            if w is not None:
                metadata[prop.name] = w.value
        before = state["metadata"]
        records = await service.list_extensions()
        extension_ids = [r.id for r in records if r.enabled and r.category == ExtensionCategory.SUGGESTION]
        try:
            await service.validate(
                item.shared_id,
                state["template"],
                state["language"],
                metadata,
                extension_ids,
                before,
            )
            _notify(f"Validated {item.title or item.shared_id}", type="positive")
        except Exception as exc:  # noqa: BLE001
            _notify(f"Validation failed: {exc}", type="negative")
            return
        await _load_lists()


def _display_value(value: Any) -> str:
    """Human-readable text for a metadata cell (titles/labels over raw ids)."""
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(_display_value(v) for v in value)
    if isinstance(value, dict):
        # Uwazi envelopes: relationship {"shared_id", "title"}, link
        # {"label", "url"}, generic {"value": ...}.
        inner = value.get("title") or value.get("label") or value.get("shared_id") or value.get("value")
        if isinstance(inner, dict):
            inner = inner.get("label") or inner.get("url")
        return str(inner) if inner is not None else ""
    return str(value)


def _scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list) and value:
        return str(value[0])
    return str(value)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _date_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(value)


def _daterange_text(value: Any) -> str:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        frm = value.get("from")
        to = value.get("to")
        if isinstance(frm, (int, float)):
            frm = _date_text(frm)
        if isinstance(to, (int, float)):
            to = _date_text(to)
        return f"{frm}->{to}"
    return str(value or "")


def _link_dict(value: Any) -> dict:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, dict):
        return value
    return {"label": str(value or ""), "url": ""}


def _geo_pair(value: Any) -> tuple[float | None, float | None]:
    if isinstance(value, list) and value and isinstance(value[0], (list, tuple)):
        pair = value[0]
        try:
            return float(pair[0]), float(pair[1])
        except (TypeError, ValueError, IndexError):
            return None, None
    return None, None


@ui.page("/")
def _index() -> None:
    _login_page()


@ui.page("/app")
def _app() -> None:
    if not _is_logged_in():
        ui.navigate.to("/")
        return
    _build_page()


def main() -> None:
    app.add_static_files("/static", str(_STATIC_DIR))

    @app.get("/pdf/{filename}")
    def _pdf(filename: str) -> Response:
        service = _current_service()
        if service is None:
            return Response(status_code=404)
        data = asyncio.run(service.get_pdf_bytes(filename))
        if data is None:
            return Response(status_code=404)
        return Response(content=data, media_type="application/pdf")

    @app.get("/viewer/{filename}")
    def _viewer(filename: str) -> Response:
        html = (_STATIC_DIR / "pdfjs" / "viewer.html").read_text()
        return Response(content=html, media_type="text/html")

    ui.run(
        host="0.0.0.0",
        port=APP_PORT,
        reload=False,
        title="Uwazi Property Filler",
        storage_secret=os.environ.get("PROPERTY_FILLER_STORAGE_SECRET", "dev-property-filler-secret"),
    )


if __name__ == "__main__":
    main()
