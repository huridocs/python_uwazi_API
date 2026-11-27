"""Web driver module split out of the original ``app`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import os
import secrets

from fastapi import Response
from loguru import logger
from nicegui import app, context, ui

from uwazi_admin_agent.drivers.links import startup_login_links
from uwazi_admin_agent.drivers.web.host_guard import assert_safe_bind
from uwazi_admin_agent.drivers.web.instances import INSTANCE_URLS, page_link
from uwazi_admin_agent.drivers.web.run_service import (
    fetch_entity_document_bytes,
)
from uwazi_admin_agent.drivers.web.session import (
    bind_client_instance,
    credentials_for_instance,
    page_instance_url,
    session_is_active,
)

from ..theme import install_theme
from .broadcast import _refresh_rows_client
from .login import _logout
from .runs_table import _build_row_menu, _build_runs_table, _selection_toolbar
from .state import _STATIC_DIR


def _build_page() -> None:
    """Build the single-page layout."""
    bind_client_instance()
    install_theme()
    with ui.header().classes("uw-header items-center justify-between"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            with ui.element("div").classes("brand-mark"):
                ui.label("u")
            with ui.column().classes("gap-0"):
                ui.label("Uwazi Admin Agent").classes("brand-name")
                ui.label("Task runs").classes("brand-sub")
            ui.element("div").classes("header-separator")
            ui.label("Tasks").classes("header-tab header-tab-active")
        with ui.row().classes("items-center gap-2 no-wrap"):
            ui.link("Open Uwazi", page_instance_url(), new_tab=True).classes("header-tab")
            with ui.button(icon="menu").props("flat round"):
                with ui.menu():
                    ui.menu_item("Log out", _logout)

    with ui.column().classes("w-full items-center"):
        with ui.element("div").classes("uw-page w-full"):
            with ui.element("div").classes("uw-card w-full"):
                with ui.row().classes("uw-card-header w-full items-center justify-between no-wrap"):
                    with ui.column().classes("gap-0"):
                        ui.label("Tasks").classes("uw-card-title")
                        ui.label(
                            "Every task generated for this instance. Open one to review and accept its suggestions."
                        ).classes("uw-card-subtitle")
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


def app_page(instance_url: str) -> None:
    """Main admin app for ONE configured instance (login required).

    The instance comes from the URL's path prefix, so a reload names its target:
    with the in-memory credentials gone (server restart, expired session) the
    operator lands on that instance's login page, never on the dummy page.
    """
    if not session_is_active(instance_url):
        ui.navigate.to(page_link("/", instance_url))
        return
    _build_page()


def main() -> None:
    """Run the NiceGUI server (uvicorn under the hood) on port 5055.

    Binds loopback by default. A deployment that needs remote access sets
    ``ADMIN_WEB_HOST`` plus an ``https://`` ``ADMIN_WEB_PUBLIC_URL`` and a
    stable ``ADMIN_WEB_STORAGE_SECRET``; :func:`assert_safe_bind` refuses the
    cleartext/wrong-secret combinations before the server starts.

    Ready time announces the login link of EVERY configured instance
    (``/<token>``), the links that actually open a login page: the path prefix
    is what selects the instance, so a bare base url is a dead end. NiceGUI's
    own welcome line (named ``show_welcome_message``) is replaced by them and
    the auto-opened browser lands on the first instance's login page instead of
    the bare path too.

    The websocket is bound to the browser session (:mod:`socket_guard`), so a
    socket that carries no session cookie cannot drive another browser's
    client.
    """
    _register_static_and_routes()
    _install_socket_guard()
    port = int(os.environ.get("ADMIN_WEB_PORT", "5055"))
    host = os.environ.get("ADMIN_WEB_HOST", "127.0.0.1")
    public_url = os.environ.get("ADMIN_WEB_PUBLIC_URL", "")
    storage_secret = _storage_secret()
    assert_safe_bind(host, public_url, storage_secret)
    user = os.environ.get("UWAZI_USER", "").strip()
    # The absolute links go to the terminal (the operator of a remote
    # deployment cannot use a loopback address); the relative ones are the
    # paths the local browser is opened at.
    base_url = public_url.rstrip("/") or f"http://{_display_host(host)}:{port}"
    login_urls = startup_login_links(base_url, INSTANCE_URLS, user)
    login_paths = startup_login_links("", INSTANCE_URLS, user)

    def _announce_login_urls() -> None:
        if login_urls:
            for login_url in login_urls:
                logger.info("Admin-agent web: open {} to log in", login_url)
        else:
            logger.warning("Admin-agent web: UWAZI_URL is empty — no instance to log in to")

    app.on_startup(_announce_login_urls)
    ui.run(
        host=host,
        port=port,
        reload=False,
        title="Uwazi Admin Agent",
        storage_secret=storage_secret,
        show=login_paths[0] if login_paths else "/",
        show_welcome_message=False,
    )


def _display_host(host: str) -> str:
    """The host a clickable URL names: a wildcard bind reads as loopback.

    ``0.0.0.0`` (or the empty/``::`` wildcards) is a bind address, never a
    destination; the printed link names the machine's own loopback so it can
    be clicked. An IPv6 literal is bracketed the way a URL requires.
    """
    if host in {"", "0.0.0.0", "::", "*"}:
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


def _install_socket_guard() -> None:
    """Bind the websocket to the browser session that owns the client.

    NiceGUI dispatches a browser event by ``client_id`` alone, so a socket
    that carries no session cookie could drive another browser's client. The
    guard refuses such a socket at connect time and refuses any event whose
    target client belongs to a different session.
    """
    from nicegui import core

    from uwazi_api.socket_guard import install

    install(core.sio)


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
