"""Review-page module split out of the original ``review_page`` (behavior preserved)."""

from __future__ import annotations

from typing import Any

from nicegui import context, ui


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
