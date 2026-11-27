"""The admin web app's configured instances: one URL path prefix per instance.

``UWAZI_URL`` may name ONE Uwazi instance or a comma-separated list; the list is
parsed once, at import. ``.env`` is loaded here, explicitly, so the configured
list never depends on which module happened to import ``drivers.runtime`` first
(``load_dotenv`` is idempotent and an already-exported value wins).
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

from uwazi_admin_agent.configuration import ROOT_PATH
from uwazi_admin_agent.domain.instance import parse_instance_urls
from uwazi_admin_agent.drivers import links
from uwazi_admin_agent.drivers.web.session import session_instance_url

load_dotenv(ROOT_PATH / ".env")

# The configured instances (one URL or a comma-separated list), in order.
INSTANCE_URLS: list[str] = parse_instance_urls(os.environ.get("UWAZI_URL"))


def page_link(path: str, instance: str | None = None) -> str:
    """An in-app URL under THIS session's instance prefix.

    ``instance`` defaults to the logged-in session's instance. The value is
    always converted to a configured token, so the URL can never carry an
    instance the app would not serve.
    """
    target = session_instance_url() if instance is None else instance
    return links.page_link(path, target, INSTANCE_URLS)


__all__ = ["INSTANCE_URLS", "page_link"]
