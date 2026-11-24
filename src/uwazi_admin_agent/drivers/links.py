"""Login-page URL helpers: instance token + username preselected.

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

MISSING_INSTANCE_MESSAGE = "Open the link that was shared with you to reach the right Uwazi instance."
UNMATCHED_INSTANCE_MESSAGE = "This link does not name a configured Uwazi instance. Check the URL you were given."
AMBIGUOUS_INSTANCE_MESSAGE = (
    "This link matches more than one configured Uwazi instance. Ask for a link that names exactly one."
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


def page_link(path: str, instance: str, configured: list[str]) -> str:
    """An in-app page URL that carries the instance token in its query string.

    ``/app``, ``/review/<id>`` and ``/task/<id>`` keep the same ``instance``
    token the login URL used, so a reload is self-describing: with the
    in-memory credentials gone, the page can send the operator back to the
    LOGIN page of the same instance instead of the bare ``/`` guidance. The
    account is NOT carried: the login page offers the username field again.
    An empty instance yields ``path`` unchanged.
    """
    token = instance_token(instance, configured) if instance else ""
    if not token:
        return path or "/"
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{urlencode({'instance': token})}"


def resolve_page_target(
    path: str,
    instance_param: str,
    user_param: str,
    session_instance: str,
    configured: list[str],
) -> tuple[str, str | None]:
    """Resolve an in-app page's URL to ``(instance to serve, redirect url or None)``.

    The page's URL and the session must agree on ONE instance:

    - no session: the page cannot serve, so redirect to the login gate and keep
      the URL's target — the gate shows that instance (or its own banner when
      the token is unknown/ambiguous) instead of the bare-path guidance;
    - a session but no token in the URL: re-enter the SAME page with the token,
      so the next reload can find its way back to this instance's login page;
    - a token naming ANOTHER instance: this session's credentials belong to a
      different host, so redirect to the login gate for the token's instance.
    """
    param = (instance_param or "").strip()
    user = (user_param or "").strip()
    if not session_instance:
        return "", login_link("", param, user) if param else "/"
    if not param:
        return session_instance, page_link(path, session_instance, configured)
    matched, error = match_instance(param, configured)
    if error is not None or matched is None:
        return "", login_link("", param, user)
    if not same_instance(matched, session_instance):
        return "", login_link("", param, user)
    return session_instance, None


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


__all__ = [
    "AMBIGUOUS_INSTANCE_MESSAGE",
    "MISSING_INSTANCE_MESSAGE",
    "UNMATCHED_INSTANCE_MESSAGE",
    "instance_token",
    "instance_tokens",
    "login_link",
    "match_instance",
    "page_link",
    "resolve_login_target",
    "resolve_page_target",
]
