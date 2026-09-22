"""Module-level per-extension failure counters for the info modal.

Adapters bump these on their failure branches; the service's
``extension_stats`` reads them alongside the ``suggestion`` row counts.
"""

_extension_error_counts: dict[str, int] = {}


def record_error(extension_id: str) -> None:
    _extension_error_counts[extension_id] = _extension_error_counts.get(extension_id, 0) + 1


def error_count(extension_id: str) -> int:
    return _extension_error_counts.get(extension_id, 0)
