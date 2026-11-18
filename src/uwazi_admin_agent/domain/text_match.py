"""Pure fuzzy-matching helpers bound into the script exec namespace (``textmatch``).

Extraction and classification over segmentation text is mostly *approximate*
string work: a document's label is "Case No." while the schema property is
``case_number``; a paragraph spells a party name "Republic of Türkiye" while the
thesaurus label is "Türkiye"; a table header says "DATE OF JUDGMENT" while the
target is "Judgment date". ``re`` alone forces the LLM to hand-roll normalisation
and scoring, which it does badly and inconsistently in every regenerated script.

This module exposes :mod:`rapidfuzz` (a C++ implementation of the standard
fuzzy-matching scorers) as a small, curated, pure API. Every function is

- deterministic (no randomness, no I/O, no training),
- cheap enough for a 10 000-entity pass (microseconds per comparison),
- JSON-friendly (plain ``float``/``dict``/``list`` results a script can store).

Scores are ``0..100`` (rapidfuzz's native scale), so a script reads a threshold
as "80 means at least 80 % similar" without any conversion.

What this deliberately is NOT: an embedding / semantic-similarity model. That
would need a model download, a heavy runtime, and non-reproducible outputs — the
sandbox forbids imports and the review table must be reproducible. Lexical
similarity over normalised text covers label/header/name matching; when a value
needs genuine judgment, the script asks the LLM (``llm_complete``/``llm_map``)
under the operator's approved budget instead.

Bound as ``textmatch`` (see
:mod:`uwazi_admin_agent.use_cases.script_exec_namespace`), next to
``htmlextract`` and ``dateparse``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from rapidfuzz import fuzz as _fuzz
from rapidfuzz import utils as _utils

#: Every scorer returns a similarity in this closed range.
SCORE_MIN: float = 0.0
SCORE_MAX: float = 100.0


class Scorer(str, Enum):
    """The named scorers a script can pass to :func:`best`/:func:`rank`.

    ``wratio`` (rapidfuzz's weighted ratio) is the default: it handles the
    partial-token case ("Case No." vs "Case number") that pure ``ratio`` scores
    low. ``token_set_ratio`` ignores token order and repetition ("Judgment date"
    vs "date of the judgment"); ``partial_ratio`` finds a short label inside a
    long paragraph.

    Every scorer normalises its inputs first (lowercase, punctuation collapsed to
    a space), so case, accents' punctuation and spacing never decide a match.
    """

    RATIO = "ratio"
    PARTIAL_RATIO = "partial_ratio"
    TOKEN_SORT_RATIO = "token_sort_ratio"
    TOKEN_SET_RATIO = "token_set_ratio"
    WRATIO = "wratio"


# Every scorer runs over NORMALISED text: lowercased, non-alphanumerics collapsed
# to single spaces. That is what makes "Case No." comparable to "case number" and
# "DATE OF JUDGMENT" to "judgment date" — the whole point of the namespace. A
# caller that needs byte-exact comparison uses the bound `str` methods instead.
_PROCESSOR = _utils.default_process

_SCORERS = {
    Scorer.RATIO: _fuzz.ratio,
    Scorer.PARTIAL_RATIO: _fuzz.partial_ratio,
    Scorer.TOKEN_SORT_RATIO: _fuzz.token_sort_ratio,
    Scorer.TOKEN_SET_RATIO: _fuzz.token_set_ratio,
    Scorer.WRATIO: _fuzz.WRatio,
}


def _text(value: Any) -> str:
    """Coerce one script value to text (``None`` becomes ``""``; pure).

    Scripts pass whatever a document/entity dict held. Numbers (a numeric
    property, a year) are common and must not raise: ``str`` keeps them
    comparable, and a non-string object with a meaningful ``__str__`` (a
    ``datetime``) still matches its rendered form.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _score(scorer: Scorer | str, a: Any, b: Any) -> float:
    key = Scorer(scorer) if not isinstance(scorer, Scorer) else scorer
    return float(_SCORERS[key](_text(a), _text(b), processor=_PROCESSOR))


def ratio(a: Any, b: Any) -> float:
    """Whole-string similarity (``0..100``): character edits, order-sensitive."""
    return _score(Scorer.RATIO, a, b)


def partial_ratio(a: Any, b: Any) -> float:
    """Best similarity of the SHORTER string against a window of the longer one.

    Use it for "does this paragraph contain this label/value?", where a plain
    :func:`ratio` would be diluted by the paragraph's remaining text.
    """
    return _score(Scorer.PARTIAL_RATIO, a, b)


def token_sort_ratio(a: Any, b: Any) -> float:
    """Token similarity after sorting both strings (order-insensitive)."""
    return _score(Scorer.TOKEN_SORT_RATIO, a, b)


def token_set_ratio(a: Any, b: Any) -> float:
    """Token similarity ignoring duplicates and extra tokens (subset-friendly)."""
    return _score(Scorer.TOKEN_SET_RATIO, a, b)


def wratio(a: Any, b: Any) -> float:
    """Weighted similarity — the best single scorer for label/header matching."""
    return _score(Scorer.WRATIO, a, b)


def rank(
    query: Any,
    choices: list[Any] | tuple[Any, ...],
    score_cutoff: float = SCORE_MIN,
    limit: int | None = None,
    scorer: Scorer | str = Scorer.WRATIO,
) -> list[dict[str, Any]]:
    """Every choice at/above ``score_cutoff``, best first (pure).

    Returns a list of ``{"choice", "score", "index"}`` dicts: ``index`` points
    back into ``choices`` so a script can map a matched label to its parallel
    payload (a thesaurus value id, a property name, a row). Ties keep input
    order. An empty ``choices`` list yields ``[]``; ``limit`` caps the result
    AFTER sorting (``None`` = no cap).
    """
    scored: list[dict[str, Any]] = []
    for index, choice in enumerate(choices):
        score = _score(scorer, query, choice)
        if score >= float(score_cutoff):
            scored.append({"choice": choice, "score": score, "index": index})
    scored.sort(key=lambda item: (-item["score"], item["index"]))
    return scored if limit is None else scored[: max(0, int(limit))]


def best(
    query: Any,
    choices: list[Any] | tuple[Any, ...],
    score_cutoff: float = SCORE_MIN,
    scorer: Scorer | str = Scorer.WRATIO,
) -> dict[str, Any] | None:
    """The single best choice at/above ``score_cutoff``, or ``None`` (pure).

    The classification idiom: map a document's free-text topic to the CLOSEST
    thesaurus label, refusing to guess when nothing clears the threshold::

        hit = textmatch.best(topic_text, labels, score_cutoff=80)
        value = hit["choice"] if hit else None
    """
    matches = rank(query, choices, score_cutoff=score_cutoff, limit=1, scorer=scorer)
    return matches[0] if matches else None


__all__ = [
    "SCORE_MAX",
    "SCORE_MIN",
    "Scorer",
    "best",
    "partial_ratio",
    "rank",
    "ratio",
    "token_set_ratio",
    "token_sort_ratio",
    "wratio",
]
