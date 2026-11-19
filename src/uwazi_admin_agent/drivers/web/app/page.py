"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import os
import secrets

from fastapi import Response
from loguru import logger
from nicegui import app, context, ui
from starlette.requests import Request

from uwazi_admin_agent.drivers.links import startup_login_url

# Import for its side effect: the ``@ui.page("/review/{run_id}")`` route registers
# on import, so the runs table's Review action can navigate to it.
from uwazi_admin_agent.drivers.web import review_page as _review_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web import task_page as _task_page_module  # noqa: F401
from uwazi_admin_agent.drivers.web.run_service import (
    fetch_entity_document_bytes,
)
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    credentials_for_instance,
    page_instance_url,
)

from .broadcast import _refresh_rows_client
from .dialogs import _capabilities_dialog, _clear_cache_dialog, _invite_links_dialog
from .login import _login_page, _logout
from .runs_table import _build_row_menu, _build_runs_table, _selection_toolbar
from .state import _INSTANCE_URLS, _STATIC_DIR, _is_logged_in


def _build_page() -> None:
    """Build the single-page layout."""
    bind_client_instance()
    ui.colors(primary="#2c3e50", secondary="#18bc9c", accent="#f39c12")
    ui.add_head_html(
        "<style>.q-stepper, .q-stepper__header, .q-stepper__step, .q-stepper__step-content, .q-stepper__step-inner, .q-stepper__content, .q-panel { width: 100% !important; max-width: none !important; }</style>"  # noqa: E501
    )
    with ui.header().classes("items-center justify-between"):
        with ui.row().classes("items-center"):
            ui.label("Uwazi Admin Agent").classes("text-h6 q-mr-md")
        with ui.row().classes("items-center"):
            ui.icon("link", color="secondary").classes("q-mr-xs")
            ui.link(page_instance_url(), page_instance_url(), new_tab=True).classes("text-white")
            with ui.row().classes("items-center q-ml-md"):
                with ui.button(icon="menu").props("flat round color=secondary"):
                    with ui.menu():
                        ui.menu_item("Capabilities", _capabilities_dialog)
                        ui.menu_item("Invite links", _invite_links_dialog)
                        ui.menu_item("Clear cache", _clear_cache_dialog)
                        ui.separator()
                        ui.menu_item("Log out", _logout)

    with ui.column().classes("w-full items-center"):
        with ui.card().classes("w-full max-w-6xl"):
            # The row above the table: bulk Delete over the checkbox selection + New Task.
            _selection_toolbar()
            table = _build_runs_table()
            context.client._runs_table = table  # noqa: SLF001 — per-client handle for in-place refresh
    _build_row_menu()

    ui.timer(5.0, _refresh_rows_client)

    # Prevent the browser back button from navigating away when a dialog is open.
    # Push a sentinel state on page load; when back is pressed while a dialog is
    # open, re-push the state and close the dialog instead of leaving the page.
    ui.add_body_html(
        """
        <script>
        (function() {
          window.history.pushState({nicegui_dialog: true}, '');
          window.addEventListener('popstate', function(e) {
            const hasDialog = document.querySelector('.q-dialog__inner:not([style*="display: none"])');
            if (hasDialog) {
              // A dialog is open: re-push state and close the dialog instead of navigating away.
              window.history.pushState({nicegui_dialog: true}, '');
              // Click the Close or Cancel button inside the dialog.
              const dlg = hasDialog.closest('.q-dialog');
              if (dlg) {
                const closeBtn = [...dlg.querySelectorAll('button')].find(b => {
                  const t = b.textContent.trim().toLowerCase();
                  return t.startsWith('close') || t === 'cancel';
                });
                if (closeBtn) closeBtn.click();
                else hasDialog.click(); // backdrop click as fallback for non-persistent dialogs
              }
            } else {
              // No dialog open: re-push so the user stays on the page.
              window.history.pushState({nicegui_dialog: true}, '');
            }
          });
        })();
        </script>
        """
    )


@ui.page("/")
def _index(request: Request) -> None:
    """Login gate: nothing renders until the Uwazi credentials validate.

    The query parameters come from the invite link: ``instance`` preselects
    the Uwazi instance (matched against the configured list) and ``user``
    preselects the account.
    """
    _login_page(
        request.query_params.get("instance", ""),
        request.query_params.get("user", ""),
    )


@ui.page("/app")
def _app() -> None:
    """Main admin app: only reachable with a validated session login."""
    if not _is_logged_in():
        ui.navigate.to("/")
        return
    _build_page()


def main() -> None:
    """Run the NiceGUI server (uvicorn under the hood) on port 5055."""
    _register_static_and_routes()
    port = int(os.environ.get("ADMIN_WEB_PORT", "5055"))
    link = startup_login_url(_INSTANCE_URLS, os.environ.get("ADMIN_WEB_PUBLIC_URL", ""), port)
    if link is not None:
        logger.info("Admin web login link (first configured instance): {}", link)
    ui.run(
        host="0.0.0.0",
        port=port,
        reload=False,
        title="Uwazi Admin Agent",
        storage_secret=_storage_secret(),
    )


def _storage_secret() -> str:
    """The session-cookie signing secret from ``ADMIN_WEB_STORAGE_SECRET``.

    A random, per-process secret is used when the variable is unset, with a
    loud warning: the browser's session id is what names the server-side
    session (and therefore the in-memory credentials), so a well-known default
    would let a leaked id be replayed as a login. A random secret still keeps
    sessions unguessable, at the cost of every session ending on restart — which
    the in-memory credential vault already implies.
    """
    secret = os.environ.get("ADMIN_WEB_STORAGE_SECRET")
    if secret:
        return secret
    logger.warning(
        "ADMIN_WEB_STORAGE_SECRET is not set — signing session cookies with a random per-start secret; "
        "all sessions (and the in-memory Uwazi credentials) end when this process restarts. "
        "Set it in the deployment to keep sessions across restarts."
    )
    return secrets.token_urlsafe(48)


def _register_static_and_routes() -> None:
    """Serve the pdf.js viewer assets and the per-entity document route.

    The viewer is the proven pdf.js setup copied from ``uwazi_property_filler``
    (its own static dir and module scripts), pointed at this app's route: the
    review window's iframe loads ``/static/pdfjs/review_viewer.html?src=…``
    with this route already URL-encoded inside, which streams the entity's
    PRIMARY document bytes through the cached file repository. ``lang`` is the
    row's language; the resolver falls back to the instance's other configured
    languages, since an entity often holds its PDF in another one.
    """
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.add_static_files("/static", str(_STATIC_DIR))

    @app.get("/review-pdf")
    def _review_pdf(run: str, sid: str, lang: str | None = None) -> Response:
        # The viewer loads this route in an iframe, so the browser presents the
        # session cookie: authentication comes from the session, never the URL.
        if not sid:
            return Response(status_code=400)
        instance = page_instance_url()
        credentials = credentials_for_instance(instance)
        if credentials is None:
            return Response(status_code=401)
        try:
            # ``lang`` is the row's language; the resolver falls back to the
            # instance's other configured languages when the entity carries no
            # document in that one.
            data = asyncio.run(fetch_entity_document_bytes(instance, credentials.user, credentials.password, sid, lang))
        except Exception as exc:  # noqa: BLE001 — a fetch failure is a 404 to the viewer
            logger.warning("review-pdf fetch failed run={} sid={} lang={}: {}", run, sid, lang, exc)
            return Response(status_code=404)
        if data is None:
            return Response(status_code=404)
        return Response(content=data, media_type="application/pdf")
