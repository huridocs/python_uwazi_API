"""Login-page and in-app URL helpers: one path prefix per Uwazi instance.

The admin web serves each configured ``UWAZI_URL`` under its own path prefix:

    http://localhost:5055/upr-info          # https://www.upr-info.org
    http://localhost:5055/girlsrightsplatform
    http://localhost:5055/localhost         # http://localhost:3000

The path prefix is a TOKEN, never a URL to dial: it is matched against the
configured ``UWAZI_URL`` list only (see :func:`match_instance`), and the value
used downstream is always the CONFIGURED url string. A path that names no
configured instance (or names several) is answered with the dummy page, so a
forged path can never point the login at another host.

Pure module: no I/O, no environment reads, no network.
"""

from __future__ import annotations

import re
from urllib.parse import urlencode, urlsplit

from uwazi_admin_agent.domain.instance import same_instance

MISSING_INSTANCE_MESSAGE = "Open the link that was shared with you to reach the right Uwazi instance."

# One path segment carries letters, digits, dots, dashes, underscores and
# colons (the port separator); a slash, a query or a bracket would not survive
# as ONE segment.
_PATH_SEGMENT = re.compile(r"^[a-z0-9._:-]+$")


def _token(value: str) -> str:
    """The normalized form of a token: trimmed, no trailing slash, lower-cased."""
    return value.strip().rstrip("/").lower()


def _host_label(host: str) -> str:
    """The first MEANINGFUL dot-label of a host (pure).

    ``www.`` is a web-fronting prefix, never an instance's name: deriving the
    label from ``www.girlsrightsplatform.org`` produced ``www``, so the natural
    route ``/girlsrightsplatform`` was refused while ``/www`` silently worked.
    """
    labels = [part for part in host.split(".") if part]
    while len(labels) > 1 and labels[0] == "www":
        labels.pop(0)
    return labels[0] if labels else ""


def _split(url: str) -> tuple[str, str, int | None, str]:
    """``(scheme, host, port, netloc)`` of a configured value (pure).

    A configured value without a scheme (``upr-info.org``, ``localhost:3000``)
    is parsed with a leading ``//`` so it is read as a host, not as a path.
    """
    parts = urlsplit(url)
    if not parts.netloc:
        parts = urlsplit("//" + url)
    try:
        port = parts.port
    except ValueError:  # an invalid port is no port
        port = None
    return parts.scheme, (parts.hostname or ""), port, parts.netloc.lower()


def instance_tokens(url: str) -> list[str]:
    """The accepted spellings of ONE configured url, in preference order.

    The host's first meaningful dot-label (``www.`` skipped: ``upr-info``), the
    host (``upr-info.org``), the host with its port (``localhost:3000``), the
    netloc as configured (``www.upr-info.org``), the legacy bare ``www`` (links
    generated before the www-aware rules carried it), a scheme-qualified form
    (``https:upr-info.org``, the last resort when two configured values share a
    host) and the normalized full url. Duplicates are dropped while order is
    preserved. A url with no host contributes only its full-url token.
    """
    normalized = _token(url)
    if not normalized:
        return []
    scheme, host, port, netloc = _split(normalized)
    if not netloc:
        return [normalized]
    bare_host = host.removeprefix("www.")
    tokens = [
        _host_label(host),
        bare_host,
        host,
        f"{host}:{port}" if port is not None else host,
        netloc,
    ]
    if netloc.startswith("www."):
        tokens.append("www")
    if scheme:
        tokens.append(f"{scheme}:{netloc}")
    tokens.append(normalized)
    unique: list[str] = []
    for token in tokens:
        if token and token not in unique:
            unique.append(token)
    return unique


def match_instance(token: str, configured: list[str]) -> str | None:
    """Resolve a path token to ONE configured url (None when it names none/several).

    Matching is EXACT against :func:`instance_tokens` (plus the normalized full
    url), so a token never silently picks an unintended instance, and an
    ambiguous token is refused rather than resolved.
    """
    normalized = _token(token)
    if not normalized:
        return None
    candidates = [url for url in configured if normalized in instance_tokens(url) or same_instance(token, url)]
    unique = list(dict.fromkeys(candidates))
    return unique[0] if len(unique) == 1 else None


def path_tokens(url: str, configured: list[str]) -> list[str]:
    """The path prefixes that resolve to EXACTLY ``url``, in preference order.

    Only single-segment spellings that :func:`match_instance` resolves to this
    instance alone: a form another configured instance also answers to would be
    an ambiguous route. The first entry is the instance's canonical path.
    """
    return [
        token
        for token in instance_tokens(url)
        if _PATH_SEGMENT.match(token) and same_instance(match_instance(token, configured) or "", url)
    ]


def instance_token(url: str, configured: list[str]) -> str:
    """The canonical path token of ``url`` ("" when it has no usable one)."""
    forms = path_tokens(url, configured)
    return forms[0] if forms else ""


def login_link(base_url: str, token: str, user: str) -> str:
    """The login URL of ONE instance token (and, optionally, one username).

    ``base_url`` is the app's origin (``https://admin.example.org``) or empty
    for the relative form (``/upr-info``). An empty ``user`` is dropped, so the
    recipient types their own username.
    """
    path = f"/{token}" if token else "/"
    query = f"?{urlencode({'user': user})}" if user else ""
    return f"{base_url.rstrip('/')}{path}{query}"


def startup_login_links(base_url: str, configured: list[str], user: str = "") -> list[str]:
    """One login URL per configured instance, in configured order.

    With no instance picker the only way to reach an instance is its own URL,
    so the console must print every one. Instances without a usable path token
    are skipped (they get no route).
    """
    links: list[str] = []
    for url in configured:
        token = instance_token(url, configured)
        if token:
            links.append(login_link(base_url, token, user))
    return links


def page_link(path: str, instance: str, configured: list[str]) -> str:
    """An in-app URL under ``instance``'s path prefix.

    ``/app`` becomes ``/upr-info/app`` and ``/`` becomes ``/upr-info`` (the
    instance's login page). Keeping the prefix in every URL is what makes a
    reload land on the same instance instead of the dummy page, even after a
    restart dropped the in-memory credentials. An empty/unknown instance and
    the path ""/"/" degrade to the unprefixed form.
    """
    token = instance_token(instance, configured) if instance else ""
    if not token:
        return path or "/"
    suffix = "" if path in ("", "/") else (path if path.startswith("/") else f"/{path}")
    return f"/{token}{suffix}"


__all__ = [
    "MISSING_INSTANCE_MESSAGE",
    "instance_token",
    "instance_tokens",
    "login_link",
    "match_instance",
    "page_link",
    "path_tokens",
    "startup_login_links",
]
