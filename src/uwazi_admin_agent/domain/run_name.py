"""Validation of a run name before it becomes a directory name.

Pure domain logic — no I/O, no environment reads. A run name is used verbatim
as the folder under the instance's ``runs/`` directory (and as the stem of its
prompt file), so a name carrying a separator would write outside that folder —
into another run's tree, or into ANOTHER INSTANCE's tree (the instance slug is
derivable from its URL). Every filesystem join on a run id goes through
:func:`validate_run_name` so that can never happen.
"""

from __future__ import annotations

_SEPARATORS = ("/", "\\")
_RESERVED = (".", "..")


def validate_run_name(name: str) -> str:
    """Return ``name`` stripped, or raise :class:`ValueError` when it is unsafe.

    A safe name is non-empty, is not ``.``/``..``, contains neither ``/`` nor
    ``\\`` (either would escape the run's folder), and carries no NUL or other
    control characters. Everything else is allowed, so existing run ids keep
    working.
    """
    value = (name or "").strip()
    if not value:
        raise ValueError("Run name is required")
    if value in _RESERVED:
        raise ValueError(f"Invalid run name: {name!r} — {value!r} is not a run name")
    if any(separator in value for separator in _SEPARATORS) or "\x00" in value:
        raise ValueError(f"Invalid run name: {name!r} — a run name cannot contain '/' or '\\'")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"Invalid run name: {name!r} — control characters are not allowed")
    return value


__all__ = ["validate_run_name"]
