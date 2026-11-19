"""Shared revert helpers."""

from __future__ import annotations


def _is_not_found(exc: Exception) -> bool:
    """True when a repository error means "this row/entity does not exist".

    The adapters raise ``RuntimeError`` with Uwazi's status/body text, so the
    match is textual (the previous behavior); kept in one place so the revert
    path never again guesses at a 404 substring inline.
    """
    message = str(exc).lower()
    return "not found" in message or "404" in message
