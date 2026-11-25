"""Isolated unit tests for the holdout validation set and its scoring (per AGENTS.md).

Pure inputs, pure outputs: no ports, no mocks, no Uwazi. These pin the rules
the feature exists to protect:

- one measured property, one metric, chosen by that property's type;
- frozen labels, never the live value;
- a missing prediction is NOT success (accuracy counts it; F1 counts FN);
- set-valued types score by micro-F1 over value occurrences;
- the store's membership flag is separate from the review lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.validation import (
    METRIC_ACCURACY,
    METRIC_F1,
    ValidationScore,
    ValidationSet,
    label_key,
    predictions_from_records,
    score_validation,
    validation_metric,
    validation_property_name,
    validation_state,
)

NOW = datetime(2026, 11, 25, tzinfo=timezone.utc)


def _validation(
    property_name: str = "document_ref",
    property_type: str = "text",
    labels: dict | None = None,
) -> ValidationSet:
    frozen = (
        labels
        if labels is not None
        else {
            label_key("a", None): "2023/111",
            label_key("b", None): "2023/222",
        }
    )
    return ValidationSet(
        run_id="run-1",
        property_name=property_name,
        property_type=property_type,
        template_names=["Judgment"],
        shared_ids=["a", "b"],
        labels=frozen,
        mode="automatic",
        created_at=NOW,
    )


def test_metric_follows_the_property_type() -> None:
    """One property, one metric: sets use micro-F1, everything else accuracy."""
    assert validation_metric("multiselect") == METRIC_F1
    assert validation_metric("relationship") == METRIC_F1
    for scalar in ("text", "select", "date", "numeric", "link", ""):
        assert validation_metric(scalar) == METRIC_ACCURACY


def test_the_measured_property_normalizes_the_title_key() -> None:
    assert validation_property_name("title") == "_title"
    assert validation_property_name("_title") == "_title"
    assert validation_property_name("document_ref") == "document_ref"


def test_accuracy_scores_frozen_labels_and_counts_missing_as_failure() -> None:
    """A held-out entity the script produced NOTHING for is not a success."""
    validation = _validation()
    score = score_validation(
        validation,
        predictions={("a", None): "2023/111"},
        script_hash="abc",
        now=NOW,
    )
    assert (score.matched, score.mismatched, score.missing) == (1, 0, 1)
    assert score.judged == 2
    assert score.value == 0.5
    assert score.metric == METRIC_ACCURACY


def test_accuracy_scores_a_wrong_value_as_a_mismatch_not_a_missing() -> None:
    validation = _validation()
    score = score_validation(
        validation,
        predictions={("a", None): "1999/000", ("b", None): "2023/222"},
        script_hash="abc",
        now=NOW,
    )
    assert (score.matched, score.mismatched, score.missing) == (1, 1, 0)
    assert score.value == 0.5


def test_a_blank_frozen_label_is_not_judged() -> None:
    """An entity the instance had no value for cannot be scored — it is excluded."""
    validation = _validation(labels={label_key("a", None): "2023/111", label_key("b", None): None})
    score = score_validation(validation, predictions={}, script_hash="abc", now=NOW)
    assert score.judged == 1
    assert score.missing == 1


def test_multiselect_scores_micro_f1_over_value_occurrences() -> None:
    """TP/FP/FN are counted per value, so a partial set is neither 0 nor 1."""
    validation = _validation(
        property_name="keywords",
        property_type="multiselect",
        labels={
            label_key("a", None): ["alpha", "beta"],
            label_key("b", None): ["gamma"],
        },
    )
    score = score_validation(
        validation,
        predictions={
            ("a", None): ["alpha", "delta"],  # TP 1 (alpha), FP 1 (delta), FN 1 (beta)
            ("b", None): ["gamma"],  # TP 1
        },
        script_hash="abc",
        now=NOW,
    )
    assert (score.tp, score.fp, score.fn) == (2, 1, 1)
    assert score.metric == METRIC_F1
    assert score.value == (2 * 2) / (2 * 2 + 1 + 1)


def test_a_missing_multiselect_prediction_is_fn_for_every_expected_value() -> None:
    validation = _validation(
        property_name="keywords",
        property_type="multiselect",
        labels={label_key("a", None): ["alpha", "beta"]},
    )
    score = score_validation(validation, predictions={}, script_hash="abc", now=NOW)
    assert (score.tp, score.fp, score.fn) == (0, 0, 2)
    assert score.value == 0.0


def test_relationship_values_compare_by_shared_id() -> None:
    """A relationship is a set of ids, not of titles: labels may collide."""
    validation = _validation(
        property_name="related",
        property_type="relationship",
        labels={label_key("a", None): [{"value": "id-1"}, {"value": "id-2"}]},
    )
    score = score_validation(
        validation,
        predictions={("a", None): [{"shared_id": "id-2"}, {"shared_id": "id-3"}]},
        script_hash="abc",
        now=NOW,
    )
    assert (score.tp, score.fp, score.fn) == (1, 1, 1)


def test_predictions_read_only_the_measured_property() -> None:
    """A validation score is about ONE property: other writes are not predictions."""
    records = [
        {"op": "update", "shared_id": "a", "language": None, "metadata": {"document_ref": "X", "other": "Y"}},
        {"op": "delete", "shared_id": "b"},
    ]
    predictions = predictions_from_records(records, "document_ref")
    assert predictions == {("a", None): "X"}


def test_the_set_version_bumps_on_membership_edits() -> None:
    validation = _validation()
    added = validation.add_entity("c", None, "2023/333")
    assert added.version == validation.version + 1
    assert "c" in added.shared_ids
    removed = added.remove_entity("a")
    assert removed.version == added.version + 1
    assert "a" not in removed.shared_ids
    assert label_key("a", None) not in removed.labels


def test_score_state_is_stale_when_the_set_changed_after_scoring() -> None:
    validation = _validation()
    score = ValidationScore(
        run_id="run-1",
        set_version=validation.version,
        script_hash="abc",
        property_name=validation.property_name,
        metric=METRIC_ACCURACY,
        value=0.5,
        judged=2,
        entities=2,
        created_at=NOW,
    )
    assert validation_state(validation, score) == "fresh"
    assert validation_state(validation.remove_entity("a"), score) == "stale"
    assert validation_state(validation, None) == "pending"
    assert validation_state(None, None) == "none"


def test_a_contaminated_set_stops_reading_as_unseen() -> None:
    validation = _validation().model_copy(update={"contaminated_at": NOW})
    score = ValidationScore(
        run_id="run-1",
        set_version=validation.version,
        script_hash="abc",
        property_name=validation.property_name,
        metric=METRIC_ACCURACY,
        value=0.9,
        judged=2,
        entities=2,
        created_at=NOW,
    )
    assert validation_state(validation, score) == "contaminated"


def test_score_rendering_states_the_metric_and_the_denominator() -> None:
    """The card never shows a bare percentage whose meaning is ambiguous."""
    validation = _validation()
    score = score_validation(
        validation,
        predictions={("a", None): "2023/111", ("b", None): "2023/222"},
        script_hash="abc",
        now=NOW,
    )
    assert score.value_text() == "100.0%"
    assert "2 checks" in score.caption("Document Ref")
    assert "not filled" in score.detail()

    f1 = score_validation(
        _validation(property_name="keywords", property_type="multiselect", labels={label_key("a", None): ["x"]}),
        predictions={("a", None): ["x"]},
        script_hash="abc",
        now=NOW,
    )
    assert f1.value_text() == "1.00 F1"
    assert "TP 1" in f1.detail()
