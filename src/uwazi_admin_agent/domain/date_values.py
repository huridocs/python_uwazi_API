"""Coerce DATE-typed property values into the API shape Uwazi accepts (pure).

Uwazi stores a date property's metadata value as an ARRAY of
``{"value": <seconds>}`` entries (its UI multiplies by 1000; its validation
refuses a bare string with ``422: metadata.date_published — Expected array,
received string``, and crashes the bulk ``/api/entities/multipleupdate`` with
a 500 for the same payload). The write contract lets the script pass a
friendlier shape ("Dates may be given as ISO strings"), which the whole-entity
write path coerces through ``uwazi_agent.adapters.uwazi_api.entity_mapper`` —
but the review table's property-scoped writes go raw, so the coercion has to
happen BEFORE the payload is built, at the OUR side of the write boundaries:

- :func:`coerce_date_property_value` is that boundary helper: given a value and
  its template property TYPE, return the numeric shape Uwazi accepts, or the
  value unchanged when it is already valid or cannot be parsed (a bad value
  must fail loudly at the write, not be silently mangled here). The
  ``{"value": ...}`` envelope is added by
  :func:`uwazi_admin_agent.use_cases.apply_suggestions_use_case.metadata_value`.

Accepts exactly what :mod:`metadata_diff` renders on read: a bare ISO string,
a numeric timestamp (seconds or milliseconds), a ``datetime``/``date``, or a
list of those (multi date) plus ``{"from", "to"}`` shapes and
``"from->to"`` strings for the range types. The ``{"value": ...}`` envelope
the on-disk shape wraps values in is unwrapped first.

No I/O, no imports outside the domain: recording (:mod:`suggestion_scoring`)
and apply (:func:`build_apply_updates`) both call this, so a review row shows
what will be sent and an Edit typed by the operator gets the same treatment.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Any

# The Uwazi property types whose value is numeric epoch seconds (see
# :mod:`uwazi_admin_agent.domain.metadata_diff`, which renders them back).
DATE_TYPES: frozenset[str] = frozenset({"date", "multidate"})
RANGE_TYPES: frozenset[str] = frozenset({"daterange", "multidaterange"})

_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")
# A value at/above this magnitude is milliseconds rather than seconds.
_MILLISECONDS_THRESHOLD = 100_000_000_000


def is_date_property_type(prop_type: str | None) -> bool:
    """Whether one Uwazi property type stores DATE-shaped values (pure)."""
    if not prop_type:
        return False
    return prop_type in DATE_TYPES or prop_type in RANGE_TYPES


def coerce_date_property_value(value: Any, prop_type: str | None) -> Any:
    """One property's value coerced to Uwazi's shape for its type (pure).

    ``date`` → ``[seconds]`` (one entry), ``multidate`` → a list of seconds,
    ``daterange`` → ``{"from": seconds, "to": seconds}``, ``multidaterange`` →
    a list of ranges. Non-date types (or ``prop_type`` unknown) return the
    value untouched: this helper must not reshape a text or select value. A
    value already in the target shape returns as-is. The caller that builds the
    raw metadata payload wraps each entry in Uwazi's ``{"value": ...}``
    envelope; this helper only produces the numeric values.
    """
    if prop_type in DATE_TYPES:
        return _coerce_dates(value)
    if prop_type in RANGE_TYPES:
        return _coerce_ranges(value)
    return value


def _coerce_dates(value: Any) -> Any:
    """A date/multidate value → the list of epoch seconds Uwazi stores (pure)."""
    if value is None:
        return []
    items = _items(value)
    if not items:
        return []
    epochs = [_to_epoch(item) for item in items]
    return [epoch for epoch in epochs if epoch is not None]


def _coerce_ranges(value: Any) -> Any:
    """A daterange/multidaterange value → Uwazi's range dict shape (pure).

    A single range (dict, ``"from->to"`` string, or a one-element list wrapping
    either) becomes ONE ``{"from": ..., "to": ...}`` dict; a list of ranges
    becomes a list of them.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        ranges = [_as_range(item) for item in value]
        found = [rng for rng in ranges if rng is not None]
        # A one-element list wrapping one range collapses to it.
        return found[0] if len(found) == 1 and len(_items(value)) == 1 else found
    return _as_range(value) or value


def _items(value: Any) -> list[Any]:
    """The coerceable scalars of a value: the list itself, else the scalar."""
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_range(item: Any) -> dict[str, Any] | None:
    """One range value (dict, ``"from->to"`` string, envelope) as a dict (pure)."""
    if isinstance(item, list) and len(item) == 1:
        item = item[0]
    if isinstance(item, dict) and "value" in item and isinstance(item["value"], dict):
        item = item["value"]
    if not isinstance(item, dict):
        if isinstance(item, str) and "->" in item:
            start, end = item.split("->", 1)
            item = {"from": start, "to": end}
        else:
            return None
    out: dict[str, Any] = {}
    if item.get("from") is not None:
        out["from"] = _to_epoch(item["from"])
    if item.get("to") is not None:
        out["to"] = _to_epoch(item["to"])
    if "from" not in out and "to" not in out:
        return None
    return out


def _to_epoch(value: Any) -> Any:
    """One scalar as an integer seconds-since-epoch (pure).

    Numeric values pass through as-is (already a timestamp). ISO strings and
    ``datetime``/``date`` objects convert in UTC. Numeric STRINGS parse too.
    Anything else — a year like ``"1983"``, prose, a bad date — returns the
    value unchanged: mangling it would be worse than letting Uwazi refuse it
    loudly.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    if isinstance(value, date):
        return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if _ISO_PREFIX.match(text):
            try:
                return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                return value
        try:
            return float(text)
        except ValueError:
            return value
    # An on-disk envelope a legacy row may carry: [{"value": "..."}].
    if isinstance(value, dict) and "value" in value:
        return _to_epoch(value["value"])
    return value
