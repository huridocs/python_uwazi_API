"""The admin web app's configured instances and the URLs that keep them.

``UWAZI_URL`` may name ONE Uwazi instance or a comma-separated list; the list
is parsed once at import. Every in-app URL carries the session's instance
``token`` in its query string (see :func:`page_link`), because the login gate
demands one: a reload that arrives with no instance in the URL can only show
the "open the link that was shared with you" guidance, and the operator loses
the instance they were working on. Carrying the token means a reload — even
one after the server dropped the in-memory credentials — lands on the LOGIN
page of that same instance instead.
"""

from __future__ import annotations

import os

from uwazi_admin_agent.domain.instance import parse_instance_urls
from uwazi_admin_agent.drivers import links
from uwazi_admin_agent.drivers.web.session import session_instance_url

# The configured instances (one URL or a comma-separated list), in order.
INSTANCE_URLS: list[str] = parse_instance_urls(os.environ.get("UWAZI_URL"))


def page_link(path: str, instance: str | None = None) -> str:
    """An in-app URL that keeps THIS session's instance in its query string.

    ``instance`` defaults to the logged-in session's instance. The value is
    always converted to a configured token, so the URL can never carry an
    instance the app would refuse to serve. The account is deliberately NOT
    carried: the login page a reload lands on must offer the username field.
    """
    # The SESSION's instance, not a page-bound one: a navigation link must name
    # the login target even from a page that is not bound yet (the login page).
    target = session_instance_url() if instance is None else instance
    return links.page_link(path, target, INSTANCE_URLS)


def page_target(path: str, instance_param: str, user_param: str) -> tuple[str, str | None]:
    """``(instance to serve, redirect url or None)`` for an in-app page request."""
    return links.resolve_page_target(path, instance_param, user_param, session_instance_url(), INSTANCE_URLS)


__all__ = ["INSTANCE_URLS", "page_link", "page_target"]
