"""Page routes: one URL prefix per configured Uwazi instance.

Every page lives under its instance's path prefix (``/upr-info``,
``/upr-info/app``, ``/upr-info/review/<run_id>``, ``/upr-info/task/<task_id>``),
so a reload names its instance and lands on that instance's login page when the
credentials are gone. Any other address answers with a minimal dummy page: with
no instance picker, the URL space must not disclose which instances exist.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from loguru import logger
from nicegui import core, ui

from uwazi_admin_agent.drivers import links
from uwazi_admin_agent.drivers.web.app.login import _guidance_page, _login_page
from uwazi_admin_agent.drivers.web.app.page import app_page
from uwazi_admin_agent.drivers.web.instances import INSTANCE_URLS
from uwazi_admin_agent.drivers.web.review_page.page import review_page
from uwazi_admin_agent.drivers.web.task_page import task_page

_DUMMY_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Uwazi Admin Agent</title>"
    "<link rel='icon' href='/favicon.ico'>"
    "<style>"
    ":root{color-scheme:light}"
    "body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;"
    "background:#f5f0e8;color:#1a1a1a;font-family:Inter,'Noto Sans',system-ui,sans-serif;padding:24px}"
    ".card{width:100%;max-width:420px;background:#fff;border:1px solid #ede7db;border-radius:12px;"
    "box-shadow:0 10px 15px -3px rgb(0 0 0 / .08);padding:28px}"
    ".brand{display:flex;align-items:center;gap:10px;margin-bottom:18px}"
    ".mark{width:28px;height:28px;border-radius:8px;background:#e8432a;color:#fff;display:flex;"
    "align-items:center;justify-content:center;font-weight:800;font-size:15px}"
    ".name{font-size:15px;font-weight:700}.sub{font-size:11px;color:#777}"
    "h1{font-size:22px;line-height:1.2;margin:0 0 8px;letter-spacing:-.02em}"
    "p{font-size:13px;line-height:1.5;color:#555;margin:0}"
    "</style></head>"
    "<body><div class='card'><div class='brand'><div class='mark'>u</div>"
    "<div><div class='name'>Uwazi Admin Agent</div><div class='sub'>Task runs</div></div></div>"
    "<h1>Instance not found</h1>"
    "<p>This address does not name a configured Uwazi instance.</p>"
    "</div></body></html>"
)


def _dummy_page(_request: Request, _exc: Exception) -> Response:
    """The answer to any address that is not one of the configured instances."""
    return HTMLResponse(_DUMMY_HTML, status_code=404)


def _login_handler(instance_url: str) -> Callable[[Request], None]:
    def handler(request: Request) -> None:
        _login_page(instance_url, request.query_params.get("user", ""))

    return handler


def _app_handler(instance_url: str) -> Callable[[], None]:
    def handler() -> None:
        app_page(instance_url)

    return handler


def _review_handler(instance_url: str) -> Callable[[str], None]:
    def handler(run_id: str) -> None:
        review_page(run_id, instance_url)

    return handler


def _task_handler(instance_url: str) -> Callable[[str], None]:
    def handler(task_id: str) -> None:
        task_page(task_id, instance_url)

    return handler


def register_routes() -> None:
    """Register the guidance page, every instance's pages, and the dummy 404."""
    ui.page("/")(_guidance_page)
    for instance_url in INSTANCE_URLS:
        forms = links.path_tokens(instance_url, INSTANCE_URLS)
        if not forms:
            logger.warning("Admin-agent web: no URL path form for {} — it is unreachable", instance_url)
            continue
        for form in forms:
            ui.page(f"/{form}")(_login_handler(instance_url))
        canonical = forms[0]
        ui.page(f"/{canonical}/app")(_app_handler(instance_url))
        ui.page(f"/{canonical}/review/{{run_id}}")(_review_handler(instance_url))
        ui.page(f"/{canonical}/task/{{task_id}}")(_task_handler(instance_url))
    core.app.add_exception_handler(404, _dummy_page)


__all__ = ["register_routes"]
