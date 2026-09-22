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

from uwazi_property_filler.configuration import APP_PORT, FILTER_PROPERTY, PROPERTY_NAME, TEMPLATE_NAME
from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_kind import ExtensionKind
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.drivers.web import service as svc
from uwazi_property_filler.use_cases.get_suggestions_use_case import merge_suggestions

_STATIC_DIR = Path(__file__).parent.parent.parent / "static"

# In-memory ring buffer of recent log lines (mirrors container stderr output).
_LOG_BUFFER: deque[str] = deque(maxlen=2000)

_CONTROLLED_UWAZI_URL = os.environ.get("UWAZI_URL", "not configured")

# Fillable property types (skip preview/nested/generatedid/image/media + title).
_SKIP_TYPES = {"preview", "nested", "generatedid", "image", "media"}

_MAX_LIST_ROWS = 1000


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


def _start_ongoing(key: str, message: str) -> None:
    """Create a dismissible ongoing notification for the current client.

    Quasar's ``ongoing`` type never auto-dismisses (``timeout: 0``), so the
    returned ``Notification`` element must be explicitly ``dismiss()``ed once
    the operation finishes.
    """
    client = _page_client()
    if client is None:
        return
    with client.layout:
        _ongoing_notifications[key] = ui.notification(
            message,
            type="ongoing",
            spinner=True,
            timeout=None,
        )


def _finish_ongoing(key: str) -> None:
    """Dismiss the ongoing notification for ``key`` and drop the handle."""
    notification = _ongoing_notifications.pop(key, None)
    if notification is not None:
        notification.dismiss()


_ongoing_notifications: dict[str, Any] = {}


def _page_client() -> Any:
    """Return the page-building client, or ``None`` outside a request."""
    try:
        return context.client
    except RuntimeError:
        return None


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
                extensions_container.clear()
                service = _current_service()
                if service is None:
                    return

                async def _load() -> None:
                    records = await service.list_extensions()
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
                    reg_error.set_text("")
                    _render_rows()
                except Exception as exc:  # noqa: BLE001
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
        "pending": [],
        "validated": [],
        "selected": None,
        "properties": [],
        "metadata": {},
        "widgets": {},
        "raw_values": {},
        "suggestions_block": None,
    }
    context.client._pf_state = state  # noqa: SLF001

    # --- 3-pane layout: left list | middle viewer | right form ------------
    with ui.splitter(value=20).classes("w-full") as outer:
        with outer.before, ui.column().classes("q-pa-sm"):
            ui.label(FILTER_PROPERTY.replace("_", " ").title()).classes("text-subtitle1")
            filter_select = ui.select({}, label="Filter", with_input=False).classes("w-full")
            state["filter_select"] = filter_select

            async def _load_filters() -> None:
                options = await service.get_filter_options(state["template"], state["language"])
                filter_select.set_options({"ALL": "ALL", **options}, value="ALL")

            background_tasks.create(_load_filters())

            def _on_filter_change(e: Any) -> None:
                value = e.value
                state["filter_value"] = None if value in (None, "ALL") else value
                if value in (None, "ALL"):
                    label = "all documents"
                else:
                    label = f'document_type = "{value}"'
                _start_ongoing("filter", f"Polling documents for {label}…")
                background_tasks.create(_load_lists())

            filter_select.on_value_change(_on_filter_change)

            with ui.tabs().classes("w-full q-mt-md") as tabs:
                pending_tab = ui.tab("pending", "Pending (0)")
                validated_tab = ui.tab("validated", "Validated (0)")
            with ui.tab_panels(tabs, value="pending").classes("w-full"):
                with ui.tab_panel("pending"):
                    pending_caption = ui.label("").classes("text-caption")
                    pending_list = ui.list().classes("w-full")
                with ui.tab_panel("validated"):
                    validated_caption = ui.label("").classes("text-caption")
                    validated_list = ui.list().classes("w-full")

            state["pending_tab"] = pending_tab
            state["validated_tab"] = validated_tab
            state["pending_caption"] = pending_caption
            state["validated_caption"] = validated_caption

            async def _load_initial() -> None:
                await _load_lists()

            background_tasks.create(_load_initial())

        with outer.after:
            with ui.splitter().classes("w-full") as inner:
                with inner.before, ui.column().classes("w-full"):
                    viewer_container = ui.column().classes("w-full")
                    state["viewer_container"] = viewer_container

                with inner.after:
                    form_container = ui.column().classes("w-full q-pa-sm")
                    state["form_container"] = form_container

    # --- list loading ------------------------------------------------------
    async def _load_lists() -> None:
        template = state["template"]
        if not template:
            return
        try:
            pending, validated = await service.list_pdfs(template, state["language"], state["filter_value"])
            state["pending"] = pending
            state["validated"] = validated
            _render_lists()
        finally:
            _finish_ongoing("filter")

    def _render_lists() -> None:
        pending_list.clear()
        validated_list.clear()
        with pending_list:
            for item in state["pending"][:_MAX_LIST_ROWS]:
                _pdf_row(item)
        with validated_list:
            for item in state["validated"][:_MAX_LIST_ROWS]:
                _pdf_row(item)
        state["pending_tab"].set_label(f"Pending ({len(state['pending'])})")
        state["validated_tab"].set_label(f"Validated ({len(state['validated'])})")
        _update_row_hint(state["pending_caption"], len(state["pending"]))
        _update_row_hint(state["validated_caption"], len(state["validated"]))

    def _update_row_hint(caption: Any, total: int) -> None:
        caption.set_text(f"showing first {_MAX_LIST_ROWS} of {total}" if total > _MAX_LIST_ROWS else "")

    def _pdf_row(item: PdfItem) -> None:
        label = item.title or item.shared_id
        with ui.item(on_click=lambda it=item: background_tasks.create(_select_pdf(it))).classes("cursor-pointer"):
            ui.item_label(label).classes("text-body2")

    async def _refresh() -> None:
        template = state["template"]
        if not template:
            _notify("Select a template first", type="warning")
            return
        _start_ongoing("refresh", "Fetching PDFs from the instance…")
        try:
            count = await service.refresh(template, state["language"], state["filter_value"])
            _notify(f"Refreshed: cached {count} PDFs", type="positive")
        except Exception as exc:  # noqa: BLE001
            _notify(f"Refresh failed: {exc}", type="negative")
        finally:
            _finish_ongoing("refresh")
        await _load_lists()

    async def _select_pdf(item: PdfItem) -> None:
        state["selected"] = item
        await _load_form()
        await _load_middle()

    async def _load_middle() -> None:
        item = state["selected"]
        if item is None:
            return
        viewer_container.clear()
        with viewer_container:
            ui.element("iframe").props(f'src="/viewer/{item.filename}" frameborder="0"').classes("w-full").style(
                "height: 70vh"
            )

    # --- right form --------------------------------------------------------
    async def _load_form() -> None:
        item = state["selected"]
        if item is None:
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

        with form_container:
            for prop in state["properties"]:
                _render_property_field(prop, metadata)
            ui.separator().classes("w-full q-my-md")
            _build_suggestions_block()
            ui.button(
                "Validate",
                icon="task_alt",
                on_click=lambda: background_tasks.create(_validate()),
            ).props("color=primary")

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

    def _build_suggestions_block() -> None:
        block = ui.column().classes("w-full")
        state["suggestions_block"] = block
        background_tasks.create(_render_suggestions())

    async def _render_suggestions() -> None:
        item = state["selected"]
        block = state["suggestions_block"]
        if item is None or block is None:
            return
        service = _current_service()
        if service is None:
            return
        properties = [p.name for p in state["properties"]]
        suggestions = await service.get_suggestions(
            item.shared_id,
            state["template"],
            state["language"],
            item.filename,
            state["metadata"],
            properties,
        )
        merged = merge_suggestions(suggestions)
        block.clear()
        with block:
            ui.label("Merged suggestions").classes("text-subtitle1")
            if not merged:
                ui.label("No suggestions.").classes("text-body2")
                return
            for prop, value in merged.items():
                ui.label(f"{prop}: {json.dumps(value, default=str)}").classes("text-body2")

            def _apply() -> None:
                for prop, value in merged.items():
                    if prop in state["raw_values"]:
                        state["raw_values"][prop] = value
                        w = state["widgets"].get(prop)
                        if w is not None:
                            w.set_text(_display_value(value))
                    else:
                        w = state["widgets"].get(prop)
                        if w is not None:
                            w.value = value

            ui.button("Apply", icon="check", on_click=_apply).props("color=secondary")

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
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
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
