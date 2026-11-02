"""The task dialogs shared by the runs table and the review page.

The SCRIPT modal belongs to both surfaces: the runs table opens it from the
row's info button, and the review page opens it from its own menu. Living it
here is what makes the two show the SAME modal — one implementation, one copy
of the script.

The modal chrome (the header with its top-right Close, the copy buttons) lives
here too, because the modals are the only things that use it.
The PROMPT modal is not here any more: it became the prompt-improvement chat
(:mod:`uwazi_admin_agent.drivers.web.prompt_chat_dialog`), which reuses this
module's chrome.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from nicegui import context, ui

from uwazi_admin_agent.drivers.web.run_service import get_run
from uwazi_admin_agent.drivers.web.session import session_instance_url


def close_button(dialog: Any, dense: bool = False) -> Any:
    """Standard modal dismiss button (X icon + label), flat."""
    button = ui.button("Close", icon="close", on_click=dialog.close).props("flat")
    if dense:
        button.props("dense")
    return button


def modal_header(dialog: Any, title: str, trailing: Callable[[], Any] | None = None) -> None:
    """Modal title row with a top-right Close button.

    The header's Close is the modal's ONLY Close: there is no bottom-right
    duplicate, so the operator has one predictable escape hatch. ``trailing``
    builds extra header widgets (e.g. Refresh / Copy all) that sit between the
    title and the Close button.

    The row is ``shrink-0``: the maximized cards are fixed-height flex columns
    whose middle section scrolls, and a compressible header would be squeezed
    to nothing once the scroll content stops shrinking (``shrink-0`` children).
    """
    with ui.row().classes("w-full items-center justify-between no-wrap shrink-0"):
        ui.label(title).classes("text-h6")
        with ui.row().classes("items-center no-wrap"):
            if trailing is not None:
                trailing()
            close_button(dialog)


def copy_to_clipboard(text: str, label: str = "Copy") -> None:
    """Button that copies ``text`` to the clipboard via the browser API.

    Uses ``navigator.clipboard.writeText`` when available (secure contexts);
    otherwise falls back to a hidden textarea + ``document.execCommand('copy')``.
    """
    payload = json.dumps(text)
    js = (
        f"navigator.clipboard ? navigator.clipboard.writeText({payload}) : (() => {{ "
        f"const el = document.createElement('textarea'); el.value = {payload}; "
        "el.style.position = 'fixed'; el.style.opacity = '0'; "
        "document.body.appendChild(el); el.select(); "
        "document.execCommand('copy'); el.remove(); })()"
    )

    def _do_copy() -> None:
        ui.run_javascript(js)
        ui.notify("Copied to clipboard", type="positive", timeout=1.5)

    ui.button(label, icon="content_copy", on_click=_do_copy).props("flat dense color=grey-7")


def script_dialog(run_id: str) -> None:
    """Modal: the run's generated Python script (only reachable when a script exists).

    Created on the page layout (not inside the refreshable table) so the 5s
    auto-refresh doesn't destroy it — same pattern as the prompt chat. The
    run's generated script lives at ``<run>/script.py``; a run whose generation
    failed has none, and the caller hides the item in that case.

    The dialog is maximized: the title row (with its top-right Close) stays
    pinned while the middle column scrolls. Every child of that scrolling column is
    ``shrink-0`` — a compressible child lets the autogrow prompt textarea (and
    the ``ui.code`` block, whose inner markdown is sized ``height: 100%``) keep
    its full content height while its box is squeezed, so the script text
    spills out of its box and paints on top of the prompt textbox. With nothing
    shrinkable the column simply scrolls instead.
    """
    try:
        detail = get_run(session_instance_url(), run_id)
    except Exception as exc:  # noqa: BLE001
        ui.notify(f"Failed to load run: {exc}", type="negative", multi_line=True)
        return
    if not detail.script:
        ui.notify("No generated script on this run.", type="warning")
        return

    with context.client.layout:
        with ui.dialog() as dialog, ui.card().classes("w-full").props("style=height:100dvh"):
            dialog.props("maximized")
            with ui.column().classes("w-full h-full items-stretch no-wrap"):
                modal_header(
                    dialog,
                    f"Generated script — {run_id}",
                    lambda: copy_to_clipboard(
                        f"Prompt:\n{detail.prompt or ''}\n\n"
                        f"Validated prompt:\n{detail.validated_prompt or ''}\n\n"
                        f"Script:\n{detail.script}",
                        "Copy all",
                    ),
                )
                with ui.column().classes("w-full grow").style("overflow-y: auto; min-height: 0"):
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        ui.label("Prompt").classes("text-subtitle1 text-grey-7")
                        copy_to_clipboard(detail.prompt or "", "Copy prompt")
                    ui.textarea(value=detail.prompt or "").classes("w-full font-mono shrink-0").props(
                        "readonly outlined autogrow"
                    ).style("min-height: 80px")
                    if detail.validated_prompt:
                        with ui.row().classes("w-full items-center justify-between shrink-0"):
                            ui.label("Validated prompt").classes("text-subtitle1 text-grey-7")
                            copy_to_clipboard(detail.validated_prompt, "Copy validated prompt")
                        ui.textarea(value=detail.validated_prompt).classes("w-full font-mono shrink-0").props(
                            "readonly outlined autogrow"
                        ).style("min-height: 80px")
                    with ui.row().classes("w-full items-center justify-between shrink-0"):
                        ui.label("Script").classes("text-subtitle1 text-grey-7")
                        copy_to_clipboard(detail.script, "Copy script")
                    ui.code(detail.script, language="python").classes("w-full shrink-0")
    dialog.open()
