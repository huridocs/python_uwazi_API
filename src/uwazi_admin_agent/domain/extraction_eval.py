"""Pure scoring of an extractor function against KNOWN values (strategy benchmark).

Choosing an extraction strategy (label lookup vs. anchored section vs. regex over
collapsed text vs. the LLM) is a measurement problem, not a taste problem: the
SAME sample set must be scored under each candidate extractor so the operator can
see which one actually recovers the values. This module is that measurement, and
it is deliberately pure — samples in, scores out — so it is unit-testable without
Uwazi, files, or a model.

The ground truth is whatever the caller supplies as ``expected`` (typically the
value the entity ALREADY carries in the target property). A field the expected
row does not carry is *unjudgeable*, not a failure: it is excluded from every
count, so a half-filled instance does not score a good extractor as bad.

Three verdicts per judged field:

- ``matched``  — the extractor returned a value equal to the known one;
- ``mismatched`` — it returned a value that is not equal (a WRONG value, which is
  the dangerous case: the review table would write it);
- ``missing``  — it returned nothing for a field the entity does carry. Safer
  than a mismatch (hard rule 3 leaves the entity untouched), but a coverage loss.

Equality is decided by a :class:`MatchPolicy`: ``exact`` (byte equality),
``normalized`` (case/whitespace/punctuation-insensitive — the default, because a
document's "Case No.  2023/111" and the stored "2023/111" are the same value to a
human), or ``fuzzy`` (``textmatch`` similarity at a threshold).
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.date_values import coerce_date_property_value, is_date_property_type
from uwazi_admin_agent.domain.suggestion_scoring import compare_values
from uwazi_admin_agent.domain.text_match import wratio


class MatchPolicy(str, Enum):
    """How a predicted value is compared with the known one.

    ``normalized`` is the REVIEW TABLE's own rule —
    :func:`~uwazi_admin_agent.domain.suggestion_scoring.compare_values`, which
    renders both sides to display text and, for a DATE property, coerces to the
    epoch seconds Uwazi stores. Benchmarking under a different rule would report
    numbers for a comparison the operator never sees.

    ``exact`` and ``fuzzy`` are stricter/looser ALTERNATIVES for a strategy check;
    a score obtained under either does not predict what the review table will show.
    """

    NORMALIZED = "normalized"
    EXACT = "exact"
    FUZZY = "fuzzy"


DEFAULT_FUZZY_CUTOFF: float = 85.0

# The one thing a caller must decide before scoring: does this value exist?
# It is NOT a comparison rule (that is the review table's), only an emptiness test.
_WHITESPACE = re.compile(r"\s+")


def is_blank(value: Any) -> bool:
    """Whether ``value`` carries nothing to judge against (pure).

    Uwazi's read envelopes are unwrapped so an empty thesaurus value
    (``[{"value": ""}]``) counts as blank while ``[{"value": "Adopted"}]`` does not.
    """
    return not _as_text(value).strip()


def _as_text(value: Any) -> str:
    """One metadata value as plain text for the blank/EXACT/FUZZY checks (pure)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return " ".join(part for part in (_as_text(item) for item in value) if part)
    if isinstance(value, dict):
        # Uwazi's read envelopes: prefer the human-readable label, then the value.
        if value.get("label") is not None:
            return _as_text(value.get("label"))
        if "value" in value:
            return _as_text(value.get("value"))
        if value.get("url"):
            return _as_text(value.get("url"))
        return json.dumps(value, sort_keys=True, default=str)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def compare_field(
    predicted: Any,
    expected: Any,
    policy: MatchPolicy = MatchPolicy.NORMALIZED,
    fuzzy_cutoff: float = DEFAULT_FUZZY_CUTOFF,
    property_type: str | None = None,
) -> bool:
    """Whether ``predicted`` counts as the known value under ``policy`` (pure).

    ``normalized`` delegates to the review table's
    :func:`~uwazi_admin_agent.domain.suggestion_scoring.compare_values`: the exact
    rendering that decides a row's Match column, so a benchmark score is the score
    the operator will read. Pass ``property_type`` for a DATE property — its stored
    value is epoch seconds, and the recording path coerces a proposed ISO string
    the same way before comparing; skipping that made a PERFECT date extractor
    score 0 %.

    ``compare_values`` returns ``None`` for "nothing to compare" (a NEW value);
    that is not a match, so it reports ``False``. Callers must still filter blank
    expectations out first (:func:`is_blank`), or a new value would read as failure.
    """
    if policy == MatchPolicy.EXACT:
        return _as_text(predicted) == _as_text(expected)
    if policy == MatchPolicy.FUZZY:
        predicted_text = _as_text(predicted)
        expected_text = _as_text(expected)
        if not predicted_text.strip() or not expected_text.strip():
            return False
        return wratio(predicted_text, expected_text) >= float(fuzzy_cutoff)
    if is_date_property_type(property_type):
        predicted = coerce_date_property_value(predicted, property_type)
        expected = coerce_date_property_value(expected, property_type)
    return compare_values(predicted, expected) is True


class FieldScore(BaseModel):
    """Verdict counts for ONE property, over the judged samples."""

    model_config = ConfigDict(frozen=True)

    property_name: str
    matched: int = 0
    mismatched: int = 0
    missing: int = 0

    @property
    def judged(self) -> int:
        return self.matched + self.mismatched + self.missing

    @property
    def accuracy(self) -> float | None:
        """matched / judged, or None when nothing was judged (pure)."""
        return self.matched / self.judged if self.judged else None

    @property
    def coverage(self) -> float | None:
        """(matched + mismatched) / judged — how often a value was produced at all."""
        produced = self.matched + self.mismatched
        return produced / self.judged if self.judged else None


class FieldMismatch(BaseModel):
    """One judged sample that did not match — the evidence a strategy needs."""

    model_config = ConfigDict(frozen=True)

    shared_id: str
    property_name: str
    expected: str
    predicted: str | None


class ExtractionScore(BaseModel):
    """The whole benchmark: per-property verdicts, examples, and the policy used."""

    model_config = ConfigDict(frozen=True)

    policy: MatchPolicy = MatchPolicy.NORMALIZED
    fuzzy_cutoff: float = DEFAULT_FUZZY_CUTOFF
    fields: dict[str, FieldScore] = Field(default_factory=dict)
    mismatches: list[FieldMismatch] = Field(default_factory=list)
    samples_total: int = 0
    samples_scored: int = 0
    samples_with_error: int = 0

    @property
    def matched(self) -> int:
        return sum(field.matched for field in self.fields.values())

    @property
    def mismatched(self) -> int:
        return sum(field.mismatched for field in self.fields.values())

    @property
    def missing(self) -> int:
        return sum(field.missing for field in self.fields.values())

    @property
    def accuracy(self) -> float | None:
        """Overall matched / judged across every property (None when unjudged)."""
        judged = self.matched + self.mismatched + self.missing
        return self.matched / judged if judged else None

    @property
    def coverage(self) -> float | None:
        """Overall (matched + mismatched) / judged (None when unjudged)."""
        judged = self.matched + self.mismatched + self.missing
        produced = self.matched + self.mismatched
        return produced / judged if judged else None


class SampleResult(BaseModel):
    """One entity's outcome: the values the candidate produced (``None`` = error)."""

    model_config = ConfigDict(frozen=True)

    shared_id: str
    expected: dict[str, Any] = Field(default_factory=dict)
    predicted: dict[str, Any] | None = None
    error: str | None = None


def score_sample(
    sample: SampleResult,
    property_names: list[str],
    policy: MatchPolicy = MatchPolicy.NORMALIZED,
    fuzzy_cutoff: float = DEFAULT_FUZZY_CUTOFF,
    property_types: dict[str, str] | None = None,
) -> tuple[dict[str, tuple[int, int, int]], list[FieldMismatch]]:
    """Verdict counts for ONE sample: ``{property: (matched, mismatched, missing)}``.

    ``property_names`` is what the extractor was asked to produce; a key it
    returns outside that set is ignored (an extractor's scratch fields are not
    scored). A sample that RAISED scores every judgeable field as missing — the
    extractor produced nothing, which is what the caller must see — and passes
    no mismatch evidence (there is no wrong value to show).
    """
    counts: dict[str, tuple[int, int, int]] = {}
    mismatches: list[FieldMismatch] = []
    predicted = sample.predicted if isinstance(sample.predicted, dict) else {}
    for name in property_names:
        expected = sample.expected.get(name)
        if is_blank(expected):
            continue  # unjudgeable: the entity does not carry a known value
        value = predicted.get(name)
        if sample.error is not None or value is None or _as_text(value).strip() == "":
            counts[name] = (0, 0, 1)
            continue
        if compare_field(
            value,
            expected,
            policy=policy,
            fuzzy_cutoff=fuzzy_cutoff,
            property_type=(property_types or {}).get(name),
        ):
            counts[name] = (1, 0, 0)
            continue
        counts[name] = (0, 1, 0)
        mismatches.append(
            FieldMismatch(
                shared_id=sample.shared_id,
                property_name=name,
                expected=_as_text(expected),
                predicted=_as_text(value),
            )
        )
    return counts, mismatches


def score_extraction(
    samples: list[SampleResult],
    property_names: list[str],
    policy: MatchPolicy = MatchPolicy.NORMALIZED,
    fuzzy_cutoff: float = DEFAULT_FUZZY_CUTOFF,
    max_mismatches: int = 20,
    property_types: dict[str, str] | None = None,
) -> ExtractionScore:
    """Aggregate per-field verdicts over every sample (pure).

    ``max_mismatches`` caps the returned evidence list; the COUNTS are never
    capped, so the accuracy figure always reflects the whole sample set.
    """
    fields: dict[str, FieldScore] = {name: FieldScore(property_name=name) for name in property_names}
    mismatches: list[FieldMismatch] = []
    scored = 0
    errored = 0
    for sample in samples:
        if sample.error is not None:
            errored += 1
        counts, sample_mismatches = score_sample(
            sample, property_names, policy=policy, fuzzy_cutoff=fuzzy_cutoff, property_types=property_types
        )
        if counts:
            scored += 1
        for name, (matched, mismatched, missing) in counts.items():
            field = fields[name]
            fields[name] = field.model_copy(
                update={
                    "matched": field.matched + matched,
                    "mismatched": field.mismatched + mismatched,
                    "missing": field.missing + missing,
                }
            )
        mismatches.extend(sample_mismatches)
    return ExtractionScore(
        policy=policy,
        fuzzy_cutoff=fuzzy_cutoff,
        fields=fields,
        mismatches=mismatches[: max(0, max_mismatches)],
        samples_total=len(samples),
        samples_scored=scored,
        samples_with_error=errored,
    )


def format_score(score: ExtractionScore, strategy: str | None = None) -> str:
    """Render one score as the compact text an agent (or operator) reads (pure)."""
    policy = score.policy.value
    if score.policy == MatchPolicy.FUZZY:
        policy += f" @ {score.fuzzy_cutoff:g}"
    headline = f"strategy: {strategy}; policy: {policy}"
    if score.accuracy is None:
        return (
            f"{headline}\nNO JUDGEABLE samples: none of the sampled entities carried a known value "
            "for the requested properties (or the property names do not match the template)."
        )
    lines = [
        headline,
        (
            f"accuracy: {100 * score.accuracy:.1f}% (matched {score.matched} / judged "
            f"{score.matched + score.mismatched + score.missing}); coverage: "
            f"{100 * (score.coverage or 0.0):.1f}% (mismatched {score.mismatched}, missing {score.missing})"
        ),
        (f"samples: {score.samples_total} scored {score.samples_scored}, extractor errors {score.samples_with_error}"),
    ]
    for name, field in score.fields.items():
        accuracy = "n/a" if field.accuracy is None else f"{100 * field.accuracy:.1f}%"
        coverage = "n/a" if field.coverage is None else f"{100 * field.coverage:.1f}%"
        lines.append(
            f"  {name}: accuracy {accuracy}, coverage {coverage} "
            f"(matched {field.matched}, mismatched {field.mismatched}, missing {field.missing})"
        )
    if score.mismatches:
        lines.append("worst mismatches (expected -> predicted):")
        lines.extend(
            f"  {item.shared_id} [{item.property_name}]: {_clip(item.expected)} -> {_clip(item.predicted)}"
            for item in score.mismatches
        )
    return "\n".join(lines)


def _clip(text: str | None, limit: int = 160) -> str:
    if text is None:
        return "None"
    return text if len(text) <= limit else text[:limit] + "…"


__all__ = [
    "DEFAULT_FUZZY_CUTOFF",
    "ExtractionScore",
    "FieldMismatch",
    "FieldScore",
    "MatchPolicy",
    "SampleResult",
    "compare_field",
    "format_score",
    "is_blank",
    "score_extraction",
    "score_sample",
]
