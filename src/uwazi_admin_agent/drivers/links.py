"""Invite links for the admin web login page: instance + username preselected.

An operator hands each instance's admins a link like

    http://localhost:5055/?instance=global-rep&user=admin

which opens the login page with that instance and that username already
filled in, so the only thing left to type is the Uwazi password.

The ``instance`` parameter is a **token**, never a URL to dial: it is matched
against the configured ``UWAZI_URL`` list only (see :func:`match_instance`),
and the value used downstream is always the CONFIGURED url string. An
unmatched token never reaches the API adapter — otherwise the login page
would be an SSRF and an admin-password exfiltration primitive.

Pure module: no I/O, no environment reads, no network.
"""

from __future__ import annotations

from urllib.parse import urlencode, urlsplit

from uwazi_admin_agent.domain.instance import same_instance

MISSING_INSTANCE_MESSAGE = (
    "The instance parameter is mandatory. Open the full link you were given, which names the Uwazi instance in the URL."
)
UNMATCHED_INSTANCE_MESSAGE = "This link does not name a configured Uwazi instance. Check the URL you were given."
AMBIGUOUS_INSTANCE_MESSAGE = (
    "This link matches more than one configured Uwazi instance. Ask for a link whose instance parameter names exactly one."
)


def _token(value: str) -> str:
    """The normalized form of a token: trimmed, no trailing slash, lower-cased."""
    return value.strip().rstrip("/").lower()


def instance_tokens(url: str) -> list[str]:
    """The accepted short forms of ONE configured url, in preference order.

    The first dot-label of the host (``global-rep``), then the lower-cased
    netloc including any port (``global-rep.uwazi.io``, ``localhost:3000``),
    then the normalized full url (``https://global-rep.uwazi.io``). A url
    with no host contributes only its full-url token. Duplicates are dropped
    while the order is preserved.
    """
    normalized = _token(url)
    if not normalized:
        return []
    netloc = urlsplit(normalized).netloc.lower()
    if not netloc:
        return [normalized]
    tokens = [netloc.split(".")[0], netloc, normalized]
    unique: list[str] = []
    for token in tokens:
        if token and token not in unique:
            unique.append(token)
    return unique


def match_instance(token: str, configured: list[str]) -> tuple[str | None, str | None]:
    """Resolve an ``instance`` token against the configured urls.

    Returns ``(configured_url, None)`` on an exact match, ``(None, message)``
    when the token names no configured instance (or names more than one), and
    ``(None, None)`` when there is no token at all.

    Matching is EXACT against the tokens of :func:`instance_tokens`, so a
    token never silently picks an unintended instance. A full url with a
    trailing slash or a differently-cased host is accepted as well.
    """
    normalized = _token(token)
    if not normalized:
        return None, None
    candidates: list[str] = []
    for url in configured:
        if normalized in instance_tokens(url) or same_instance(token, url):
            candidates.append(url)
    unique = list(dict.fromkeys(candidates))
    if not unique:
        return None, UNMATCHED_INSTANCE_MESSAGE
    if len(unique) > 1:
        return None, AMBIGUOUS_INSTANCE_MESSAGE
    return unique[0], None


def instance_token(url: str, configured: list[str]) -> str:
    """The token a generated link carries for ``url``.

    The shortest form of :func:`instance_tokens` that resolves back to THIS
    instance alone; the normalized full url when even that is ambiguous
    (e.g. two configured hosts share their first dot-label).
    """
    for token in instance_tokens(url):
        matched, error = match_instance(token, configured)
        if error is None and matched is not None and same_instance(matched, url):
            return token
    return _token(url)


def login_link(base_url: str, instance: str, user: str) -> str:
    """The login link for one instance (and, optionally, one username).

    Empty values are dropped, so an empty ``user`` yields a link that asks
    the recipient for their username. An empty ``base_url`` yields the
    relative form ``/?instance=…``.
    """
    query = urlencode({key: value for key, value in (("instance", instance), ("user", user)) if value})
    prefix = f"{base_url.rstrip('/')}/" if base_url else "/"
    return f"{prefix}?{query}"


def login_links(
    configured: list[str],
    base_url: str,
    user: str,
    instance: str | None = None,
) -> list[str]:
    """The login links for the configured instances, in configured order.

    ``instance`` selects ONE instance (by url or by short token); an
    unmatched or ambiguous value yields no links at all.
    """
    if instance is None:
        urls = list(configured)
    else:
        matched, error = match_instance(instance, configured)
        if error is not None or matched is None:
            return []
        urls = [matched]
    return [login_link(base_url, instance_token(url, configured), user) for url in urls]


def resolve_login_target(
    instance_param: str,
    user_param: str,
    configured: list[str],
) -> tuple[str, str, str | None]:
    """Resolve the login page's query parameters to (instance, user, error).

    The instance is ALWAYS a configured url string or empty — never the raw
    parameter — so an unmatched token can never reach the API adapter. The
    ``instance`` parameter is MANDATORY: an empty one is reported as an error
    instead of falling back to the first configured instance, so a bare ``/``
    shows guidance rather than a login form. The error is None when the target
    is usable.
    """
    user = (user_param or "").strip()
    if not configured:
        return "", user, None
    if not (instance_param or "").strip():
        return "", user, MISSING_INSTANCE_MESSAGE
    matched, error = match_instance(instance_param, configured)
    if error is not None or matched is None:
        return "", user, error
    return matched, user, None


def startup_login_url(configured: list[str], base_url: str, port: int) -> str | None:
    """The login link printed at startup for the FIRST configured instance.

    The username is left out: the receiving admin types their own. An empty
    ``base_url`` yields a locally reachable absolute url (a relative link is
    useless as a terminal message).
    """
    if not configured:
        return None
    query = urlencode({"instance": instance_token(configured[0], configured)})
    if base_url:
        return f"{base_url.rstrip('/')}/?{query}"
    return f"http://localhost:{port}/?{query}"


__all__ = [
    "AMBIGUOUS_INSTANCE_MESSAGE",
    "MISSING_INSTANCE_MESSAGE",
    "UNMATCHED_INSTANCE_MESSAGE",
    "instance_token",
    "instance_tokens",
    "login_link",
    "login_links",
    "match_instance",
    "resolve_login_target",
    "startup_login_url",
]
