"""The credential rule: a Uwazi login needs BOTH a username and a password.

Pure module — no I/O, no environment reads. Two rules live here so they cannot
drift between the web gates, the CLIs and the HTTP adapter:

- a login is only ATTEMPTED when both credentials are present and non-blank;
  an empty username or password never reaches the Uwazi instance;
- a login GATE (a web form, a CLI, a live test) REFUSES an empty credential
  instead of sending it.

There is deliberately no default account. The admin agent's web login and the
property filler's web login used to fall back to ``admin``/``admin``, which
turned a missing credential into a working login on any instance that still
ships the default credentials.
"""

from __future__ import annotations

from uwazi_api.domain.exceptions import MissingCredentialsError

MISSING_CREDENTIALS_MESSAGE = (
    "A Uwazi username and password are required. Type them in, or set UWAZI_USER and "
    "UWAZI_PASSWORD — no default account is ever assumed."
)


def has_credentials(user: str | None, password: str | None) -> bool:
    """True when BOTH a username and a password are present and non-blank.

    Blankness is judged on the stripped value (a stray space is not a
    username), but the credentials themselves are never modified: a password
    may legitimately contain leading or trailing spaces.
    """
    return bool((user or "").strip()) and bool((password or "").strip())


def require_credentials(user: str | None, password: str | None) -> tuple[str, str]:
    """Return the credentials unchanged, refusing an empty one.

    Raises :class:`MissingCredentialsError` when either the username or the
    password is missing or blank.
    """
    if not has_credentials(user, password):
        raise MissingCredentialsError(MISSING_CREDENTIALS_MESSAGE)
    return str(user), str(password)


__all__ = [
    "MISSING_CREDENTIALS_MESSAGE",
    "MissingCredentialsError",
    "has_credentials",
    "require_credentials",
]
