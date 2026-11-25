"""The holdout validation set and its score (pure).

A validation set is a fixed sample of entities the generation agent is NOT
allowed to read. The set is resolved to concrete ``sharedId``s when a run is
created, and the expected value of ONE measured property is frozen with it. The
run's script is then evaluated against those entities in a separate planning
pass whose rows are marked ``is_validation`` and can never be run.

Why the value is frozen: the automatic label is the value Uwazi already holds
at freeze time. Re-reading it after a Run (or after an external edit) would
turn the measurement into a comparison against the model's own output.

The metric is chosen by the measured property's type:
- ``multiselect`` / ``relationship`` are VALUE SETS, scored with micro-F1
  (``2*sum(TP) / (2*sum(TP) + sum(FP) + sum(FN))``);
- every other type is scored with the review table's own normalized accuracy
  (``compare_field``), so the number matches what the operator sees in a row.

Everything here is pure: inputs in, score out. The use cases fetch samples and
persist rows; this module only decides what the score IS.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.extraction_eval import MatchPolicy, compare_field, is_blank
from uwazi_admin_agent.domain.relationship_values import shared_ids_of
from uwazi_admin_agent.domain.select_values import labels_of
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY
from uwazi_admin_agent.domain.suggestion_scoring import value_in_language

VALIDATION_NONE = "none"
VALIDATION_AUTOMATIC = "automatic"
VALIDATION_CUSTOM = "custom"
VALIDATION_MODES: tuple[str, ...] = (VALIDATION_NONE, VALIDATION_AUTOMATIC, VALIDATION_CUSTOM)
ValidationMode = Literal["none", "automatic", "custom"]

METRIC_ACCURACY = "accuracy"
METRIC_F1 = "f1_micro"
ValidationMetric = Literal["accuracy", "f1_micro"]

# Property types whose value is a SET. Micro-F1 is defined per value occurrence,
# so these are the only types where it is the honest metric.
SET_VALUED_PROPERTY_TYPES: frozenset[str] = frozenset({"multiselect", "relationship"})

# The wizard's display name for the entity title is "title"; the store's
# synthetic property row is "_title". Normalize at the boundary so both the
# frozen labels and the suggestion rows speak the same key.
_TITLE_ALIASES: frozenset[str] = frozenset({"title", TITLE_PROPERTY})


def validation_property_name(name: str) -> str:
    """The store's property key for a measured property (pure)."""
    value = (name or "").strip()
    return TITLE_PROPERTY if value in _TITLE_ALIASES else value


def validation_metric(property_type: str) -> ValidationMetric:
    """The metric a property type is scored with (pure)."""
    return METRIC_F1 if (property_type or "").strip().lower() in SET_VALUED_PROPERTY_TYPES else METRIC_ACCURACY


def label_key(shared_id: str, language: str | None) -> str:
    """The frozen-label key: one value per (entity, language) (pure)."""
    return f"{shared_id}|{language or ''}"


def split_label_key(key: str) -> tuple[str, str | None]:
    """Inverse of :func:`label_key` (pure)."""
    shared_id, _sep, language = key.partition("|")
    return shared_id, language or None


class ValidationConfig(BaseModel):
    """What the wizard chose about validation (intent only; no I/O)."""

    model_config = ConfigDict(frozen=True)

    mode: ValidationMode = VALIDATION_NONE
    property_name: str = Field(default="", description="The ONE property the score measures.")
    property_type: str = Field(default="", description="The measured property's Uwazi type.")
    template_names: list[str] = Field(default_factory=list, description="Templates the sample is drawn from.")
    sample_size: int = Field(default=30, ge=1, description="How many entities automatic sampling asks for.")
    filter_text: str = Field(default="", description="Free-text search used to resolve a custom sample.")
    language: str = Field(default="en", description="Entity row the sample and its labels are read in.")

    def resolved_property_name(self) -> str:
        """The store key of the measured property (pure)."""
        return validation_property_name(self.property_name)


class ValidationSet(BaseModel):
    """A run's frozen holdout: its entities, measured property, and labels."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    property_name: str
    property_type: str
    template_names: list[str] = Field(default_factory=list)
    shared_ids: list[str] = Field(default_factory=list)
    labels: dict[str, Any] = Field(
        default_factory=dict,
        description="label_key(sharedId, language) -> the expected value at freeze time.",
    )
    mode: ValidationMode = VALIDATION_AUTOMATIC
    filter_text: str = ""
    version: int = Field(default=1, ge=1, description="Bumped on every membership/label edit; scores are per version.")
    contaminated_at: datetime | None = Field(
        default=None,
        description=(
            "Set when a sample was added AFTER generation had already read it. The score is then a regression "
            "score, not a fair unseen estimate, and the UI says so."
        ),
    )
    created_at: datetime

    def is_per_language(self) -> bool:
        """Whether the measured property stores one value per language row (pure)."""
        return (self.property_type or "").strip().lower() in {"text", "richtext"}

    def add_entity(self, shared_id: str, language: str | None, value: Any) -> "ValidationSet":
        """A copy with one frozen sample added (pure)."""
        ids = list(self.shared_ids)
        if shared_id not in ids:
            ids.append(shared_id)
        labels = dict(self.labels)
        labels[label_key(shared_id, language)] = value
        return self.model_copy(
            update={
                "shared_ids": ids,
                "labels": labels,
                "version": self.version + 1,
            }
        )

    def remove_entity(self, shared_id: str) -> "ValidationSet":
        """A copy with one sample removed (all its frozen languages) (pure)."""
        return self.model_copy(
            update={
                "shared_ids": [sid for sid in self.shared_ids if sid != shared_id],
                "labels": {key: value for key, value in self.labels.items() if split_label_key(key)[0] != shared_id},
                "version": self.version + 1,
            }
        )


class ValidationScore(BaseModel):
    """One evaluation of a script version against one validation-set version."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    set_version: int
    script_hash: str
    property_name: str
    metric: ValidationMetric
    value: float
    matched: int = 0
    mismatched: int = 0
    missing: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    judged: int = 0
    entities: int = 0
    created_at: datetime

    def value_text(self) -> str:
        """The headline figure, with its metric (pure)."""
        if self.metric == METRIC_F1:
            return f"{self.value:.2f} F1"
        return f"{100 * self.value:.1f}%"

    def caption(self, measured_property: str) -> str:
        """The one-line reading under the headline (pure)."""
        checks = "check" if self.judged == 1 else "checks"
        return (
            f"{self.judged} {checks} on {self.entities} held-out "
            f"{'entity' if self.entities == 1 else 'entities'} · measured: {measured_property}"
        )

    def detail(self) -> str:
        """The verdict counts, for the tooltip / dialog (pure)."""
        if self.metric == METRIC_F1:
            return f"TP {self.tp} · FP {self.fp} · FN {self.fn}"
        return f"matched {self.matched} · mismatched {self.mismatched} · not filled {self.missing}"


def _set_of(value: Any, property_type: str) -> set[str]:
    """The comparable value set of one expected/predicted value (pure)."""
    if value is None:
        return set()
    if (property_type or "").strip().lower() == "relationship":
        items = shared_ids_of(value)
    else:
        items = labels_of(value)
    return {str(item).strip() for item in items if str(item).strip()}


def score_validation(
    validation: ValidationSet,
    predictions: Mapping[tuple[str, str | None], Any],
    script_hash: str,
    now: datetime,
) -> ValidationScore:
    """Score one script version over the frozen labels (pure).

    ``predictions`` is keyed by the (sharedId, language) pair a row writes. A
    pair absent from the mapping is a MISSING prediction: the script produced
    no value for that held-out entity, which for a fill task is not success.
    A blank frozen label is excluded (the entity is not judgeable).
    """
    metric = validation_metric(validation.property_type)
    matched = mismatched = missing = tp = fp = fn = judged = 0
    entities: set[str] = set()
    for key, expected in validation.labels.items():
        if is_blank(expected):
            continue
        shared_id, language = split_label_key(key)
        predicted = predictions.get((shared_id, language))
        judged += 1
        entities.add(shared_id)
        if metric == METRIC_F1:
            expected_set = _set_of(expected, validation.property_type)
            predicted_set = _set_of(predicted, validation.property_type)
            tp += len(expected_set & predicted_set)
            fp += len(predicted_set - expected_set)
            fn += len(expected_set - predicted_set)
            continue
        if is_blank(predicted):
            missing += 1
            continue
        if compare_field(
            predicted,
            expected,
            policy=MatchPolicy.NORMALIZED,
            property_type=validation.property_type,
        ):
            matched += 1
        else:
            mismatched += 1

    if metric == METRIC_F1:
        denominator = 2 * tp + fp + fn
        value = (2 * tp / denominator) if denominator else 0.0
    else:
        denominator = matched + mismatched + missing
        value = (matched / denominator) if denominator else 0.0
    return ValidationScore(
        run_id=validation.run_id,
        set_version=validation.version,
        script_hash=script_hash,
        property_name=validation.property_name,
        metric=metric,
        value=value,
        matched=matched,
        mismatched=mismatched,
        missing=missing,
        tp=tp,
        fp=fp,
        fn=fn,
        judged=judged,
        entities=len(entities),
        created_at=now,
    )


# The dry-run record ops whose ``metadata`` carries a proposed value.
_VALUE_RECORD_OPS: frozenset[str] = frozenset({"update", "create", "update_and_delete", "create_and_delete"})


def script_hash(script: str) -> str:
    """A stable short hash of a script source (pure).

    The score is versioned by it: a regeneration that changes the script must
    not silently inherit the previous number.
    """
    return hashlib.sha256(script.encode("utf-8")).hexdigest()[:16]


def predictions_from_records(records: list[dict[str, Any]], property_name: str) -> dict[tuple[str, str | None], Any]:
    """The proposed value per ``(sharedId, language)`` for ONE property (pure).

    Only the measured property is read: a validation set scores one property by
    design, so a record that writes another one contributes nothing. A create
    record's placeholder id cannot collide with a frozen sample (the set is
    built from ids Uwazi already had).
    """
    predictions: dict[tuple[str, str | None], Any] = {}
    for record in records:
        if str(record.get("op") or "") not in _VALUE_RECORD_OPS:
            continue
        shared_id = str(record.get("shared_id") or "")
        if not shared_id:
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, dict) or property_name not in metadata:
            continue
        language = record.get("language")
        language = language if isinstance(language, str) and language else None
        predictions[(shared_id, language)] = value_in_language(metadata.get(property_name), language)
    return predictions


def validation_state(validation: ValidationSet | None, score: ValidationScore | None) -> str:
    """The score card's state word (pure).

    ``none`` — the run has no validation set; ``pending`` — a set exists but no
    script has been scored against it; ``stale`` — the set changed after the
    score (membership edit, promotion); ``contaminated`` — a sample was used for
    a prompt fix, so the number is a regression score, not unseen performance;
    ``fresh`` — a fair held-out score.
    """
    if validation is None:
        return "none"
    if score is None:
        return "pending"
    if score.set_version != validation.version:
        return "stale"
    if validation.contaminated_at is not None:
        return "contaminated"
    return "fresh"


__all__ = [
    "METRIC_ACCURACY",
    "METRIC_F1",
    "SET_VALUED_PROPERTY_TYPES",
    "VALIDATION_AUTOMATIC",
    "VALIDATION_CUSTOM",
    "VALIDATION_MODES",
    "VALIDATION_NONE",
    "ValidationConfig",
    "ValidationMode",
    "ValidationScore",
    "ValidationSet",
    "label_key",
    "predictions_from_records",
    "script_hash",
    "score_validation",
    "split_label_key",
    "validation_metric",
    "validation_property_name",
    "validation_state",
]
