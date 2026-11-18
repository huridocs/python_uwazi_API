"""Pure, deterministic date detection bound into the script namespace (``dateparse``).

Extraction tasks over segmentation text constantly need one thing pure ``re``
does badly: *the* date a document states ("Judgment of 17 November 2026",
"2026-11-17", "11/17/2026") rendered as the ISO string a Uwazi ``date`` property
writes (``"YYYY-MM-DD"``). Hand-rolled regexes either miss the long form or
capture a case number; a generic parser in ``fuzzy`` mode silently invents dates
from arbitrary text (``"section 4.2"`` -> a real calendar day), which would write
fabricated metadata.

This module is the curated answer: :mod:`python-dateutil`'s parser behind an
explicit **candidate-scan gate**. A value is only accepted when

1. the text contains a date-SHAPED token (an ISO date, a ``d/m/y`` numeric date,
   or a day + English month name + year), AND
2. the token parses WITHOUT fuzzy matching, AND
3. (by default) the token names a year explicitly.

So a false positive requires a genuine date-shaped substring — never a stray
number in prose. Ambiguous ``dd/mm`` vs ``mm/dd`` follows ``dayfirst``
(``False`` = US order, the Uwazi/DOM default); an unambiguous token (``17/11``)
ignores the flag.

NOT a learned NER model: no model download, no weights, no non-determinism —
a generated script must be reproducible, and the review table must show what a
rerun would show. English month names only (dateutil's limit); a script that
needs another language should match the numeric form or ask the LLM under the
approved budget.

Bound as ``dateparse`` (see
:mod:`uwazi_admin_agent.use_cases.script_exec_namespace`), next to
``htmlextract`` and ``textmatch``.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as _dateutil

# `YYYY-MM-DD` (the shape Uwazi writes and every other shape normalises to).
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
# `d/m/y` or `d.m.y` — 2- or 4-digit year, 1- or 2-digit day/month. A trailing
# sentence period is allowed; another digit right after the year is not (a
# version-like run, not a date).
_NUMERIC_RE = re.compile(r"(?<![\d.])(\d{1,2})[/.](\d{1,2})[/.](\d{2}|\d{4})(?!\d)")
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December"
    "|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
# `17 November 2026`, `17th November, 2026`, `17 Nov. 2026`.
_DAY_MONTH_RE = re.compile(
    rf"(?<![\w.])(\d{{1,2}})(?:st|nd|rd|th)?\.?\s+({_MONTHS})\.?,?\s+(\d{{4}})(?!\d)",
    re.IGNORECASE,
)
# `November 17, 2026`, `Nov. 17 2026`.
_MONTH_DAY_RE = re.compile(
    rf"(?<![\w.])({_MONTHS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)",
    re.IGNORECASE,
)

# A date property's value is a calendar DAY; anything outside this range is a
# parse artifact (year 1 in an ISO-like token, a 4-digit case number) and is
# refused rather than written.
_MIN_YEAR: int = 1000
_MAX_YEAR: int = 2999

#: One detected date: ``{"iso", "text", "start", "end"}`` (offsets into the input).
DateHit = dict[str, Any]


def _valid_year(year: int) -> bool:
    return _MIN_YEAR <= year <= _MAX_YEAR


def _to_iso(year: int, month: int, day: int) -> str | None:
    """``YYYY-MM-DD`` for a real calendar day, or ``None`` (pure).

    Rejects out-of-range components and impossible days (``2026-02-30``).
    """
    if not _valid_year(year) or not 1 <= month <= 12 or not 1 <= day <= 31:
        return None
    try:
        # dateutil validates by constructing a datetime; no arithmetic here.
        parsed = _dateutil.parse(f"{year:04d}-{month:02d}-{day:02d}")
    except (ValueError, OverflowError):
        return None
    if (parsed.year, parsed.month, parsed.day) != (year, month, day):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _numeric_iso(match: re.Match[str], dayfirst: bool) -> str | None:
    first, second, year_text = match.group(1), match.group(2), match.group(3)
    year = int(year_text)
    if len(year_text) == 2:
        # Two-digit years are century-ambiguous; refuse rather than guess.
        return None
    a, b = int(first), int(second)
    if a > 12 and b > 12:
        return None
    if a > 12:
        day, month = a, b
    elif b > 12:
        day, month = b, a
    else:
        day, month = (a, b) if dayfirst else (b, a)
    return _to_iso(year, month, day)


def _parse_token(match: re.Match[str], dayfirst: bool) -> str | None:
    """The ISO date one month-name token states, or ``None`` (pure)."""
    try:
        parsed = _dateutil.parse(match.group(0), dayfirst=dayfirst, fuzzy=False)
    except (ValueError, OverflowError):
        return None
    return _to_iso(parsed.year, parsed.month, parsed.day)


def parse(text: Any, dayfirst: bool = False) -> str | None:
    """The ONE date a short string states, as ``YYYY-MM-DD``, or ``None`` (pure).

    The extraction idiom: a table cell holding a judgment date. ``None`` means
    "no unambiguous date here" — never write a guessed value, leave the entity
    untouched. ``dayfirst`` picks the order for an ambiguous ``d/m/y`` token.
    """
    hits = find(text, dayfirst=dayfirst, limit=1)
    return hits[0]["iso"] if hits else None


def find(text: Any, dayfirst: bool = False, limit: int | None = None) -> list[DateHit]:
    """Every date-shaped, non-fuzzy-parsable date in ``text``, in reading order (pure).

    Each hit is ``{"iso": "YYYY-MM-DD", "text": <the matched substring>,
    "start": int, "end": int}``. Use it to pick a date out of a paragraph::

        hits = dateparse.find(paragraph_text)
        judgment_date = hits[0]["iso"] if hits else None

    Overlapping candidate shapes are resolved to the FIRST (leftmost, then
    longest) match, so ``2026-11-17`` is not also reported as a numeric token.
    """
    if not isinstance(text, str) or not text:
        return []
    if limit is not None and limit <= 0:
        return []
    candidates: list[tuple[int, int, str]] = []
    for match in _ISO_RE.finditer(text):
        iso = _to_iso(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if iso is not None:
            candidates.append((match.start(), match.end(), iso))
    for match in _NUMERIC_RE.finditer(text):
        iso = _numeric_iso(match, dayfirst)
        if iso is not None:
            candidates.append((match.start(), match.end(), iso))
    for pattern in (_DAY_MONTH_RE, _MONTH_DAY_RE):
        for match in pattern.finditer(text):
            iso = _parse_token(match, dayfirst)
            if iso is not None:
                candidates.append((match.start(), match.end(), iso))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    hits: list[DateHit] = []
    consumed_until = -1
    for start, end, iso in candidates:
        if start < consumed_until:
            continue
        consumed_until = end
        hits.append({"iso": iso, "text": text[start:end], "start": start, "end": end})
        if limit is not None and len(hits) >= limit:
            break
    return hits


def is_date(text: Any, dayfirst: bool = False) -> bool:
    """Whether ``text`` states an unambiguous date (pure; the boolean form)."""
    return parse(text, dayfirst=dayfirst) is not None


__all__ = ["DateHit", "find", "is_date", "parse"]
