"""Isolated unit tests for the extraction strategy benchmark scoring (per AGENTS.md).

Pure scoring over literal samples: no mocks, no ports, no LLM, no Uwazi. These pin
the decisions the benchmark exists to protect — a WRONG value counts as a
mismatch (never folded into "missing"), an unjudgeable field is excluded instead
of failing a candidate, and the accuracy figure is computable per property.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.extraction_eval import (
    MatchPolicy,
    SampleResult,
    compare_field,
    format_score,
    is_blank,
    score_extraction,
    score_sample,
)


def _sample(shared_id: str, expected: dict, predicted: dict | None, error: str | None = None) -> SampleResult:
    return SampleResult(shared_id=shared_id, expected=expected, predicted=predicted, error=error)


def test_normalized_comparison_is_the_review_tables_own_rule() -> None:
    """The benchmark must score the comparison the operator will SEE: the review
    table renders both sides to display text (``compare_values``). A hand-rolled
    normaliser drifted from it — it called "2023-111" equal to "2023/111", which the
    table reports as a mismatch."""
    from uwazi_admin_agent.domain.suggestion_scoring import compare_values

    for proposed, current in (
        ("2023/111", "2023/111"),
        ("Doe", ["Doe"]),
        ("2023-111", "2023/111"),
        ("Türkiye", "Turkiye"),
    ):
        assert compare_field(proposed, current) is (compare_values(proposed, current) is True), proposed


def test_normalized_comparison_still_separates_different_values() -> None:
    assert compare_field("2023/111", "2023/222") is False


def test_date_property_scores_an_iso_extraction_against_epoch_seconds() -> None:
    """THE regression: a date property stores epoch seconds. Comparing an ISO
    extraction to that raw value reports a PERFECT extractor as 0 %, which would
    make the benchmark reject exactly the strategy request (2) exists to support."""
    epoch = int(datetime(2026, 11, 17, tzinfo=timezone.utc).timestamp())

    assert compare_field("2026-11-17", epoch, property_type="date") is True
    assert compare_field("2026-11-17", [{"value": epoch}], property_type="date") is True
    assert compare_field("2026-11-18", epoch, property_type="date") is False


def test_date_scoring_still_works_without_a_known_property_type() -> None:
    """No template resolved: the plain rendering rule applies (ISO vs ISO), so a
    missing type refines nothing but never crashes the pass."""
    assert compare_field("2026-11-17", "2026-11-17") is True


def test_exact_policy_is_byte_equality() -> None:
    assert compare_field("2023/111", "2023/111", policy=MatchPolicy.EXACT) is True
    assert compare_field("2023/111 ", "2023/111", policy=MatchPolicy.EXACT) is False


def test_fuzzy_policy_accepts_a_near_match_and_rejects_a_distant_one() -> None:
    assert compare_field("Republic of Turkiye", "Republic of Türkiye", policy=MatchPolicy.FUZZY, fuzzy_cutoff=90) is True
    assert compare_field("Greece", "Republic of Türkiye", policy=MatchPolicy.FUZZY, fuzzy_cutoff=90) is False


def test_empty_prediction_fails_every_policy() -> None:
    for policy in MatchPolicy:
        assert compare_field(None, "2023/111", policy=policy) is False
        assert compare_field("", "2023/111", policy=policy) is False


def test_blank_detection_unwraps_read_envelopes() -> None:
    """An unjudgeable field is excluded, so the emptiness test must see through
    Uwazi's ``[{"value": ...}]`` envelope."""
    assert is_blank(None) is True
    assert is_blank("   ") is True
    assert is_blank([{"value": ""}]) is True
    assert is_blank([{"value": "Adopted"}]) is False
    assert is_blank(["2023/111"]) is False


def test_score_sample_counts_matched_mismatched_and_missing_separately() -> None:
    """The dangerous split: producing a WRONG value (mismatched) is reported apart
    from producing nothing (missing) — only the former would write bad data."""
    sample = _sample("a", {"case_number": "2023/111", "judge": "Doe"}, {"case_number": "2023/999"})

    counts, mismatches = score_sample(sample, ["case_number", "judge"])

    assert counts["case_number"] == (0, 1, 0)
    assert counts["judge"] == (0, 0, 1)
    assert len(mismatches) == 1
    assert mismatches[0].expected == "2023/111"
    assert mismatches[0].predicted == "2023/999"


def test_score_sample_matched_when_the_value_agrees() -> None:
    sample = _sample("a", {"case_number": "2023/111"}, {"case_number": "2023/111"})

    counts, mismatches = score_sample(sample, ["case_number"])

    assert counts["case_number"] == (1, 0, 0)
    assert mismatches == []


def test_unjudgeable_field_is_excluded_not_counted_as_a_failure() -> None:
    """A half-filled instance must not score a good extractor as bad."""
    counts, mismatches = score_sample(_sample("a", {"case_number": None}, {"case_number": "anything"}), ["case_number"])

    assert counts == {}
    assert mismatches == []


def test_keys_outside_the_scored_property_set_are_ignored() -> None:
    counts, _ = score_sample(_sample("a", {"case_number": "1"}, {"case_number": "1", "scratch": "x"}), ["case_number"])

    assert set(counts) == {"case_number"}


def test_a_candidate_error_scores_every_judgeable_field_as_missing() -> None:
    counts, mismatches = score_sample(
        _sample("a", {"case_number": "2023/111"}, None, error="ValueError: boom"), ["case_number"]
    )

    assert counts["case_number"] == (0, 0, 1)
    assert mismatches == []


def test_score_extraction_aggregates_per_property_accuracy_and_coverage() -> None:
    samples = [
        _sample("a", {"case_number": "2023/111"}, {"case_number": "2023/111"}),
        _sample("b", {"case_number": "2023/222"}, {"case_number": "2023/999"}),
        _sample("c", {"case_number": "2023/333"}, None),
    ]

    score = score_extraction(samples, ["case_number"])

    field = score.fields["case_number"]
    assert (field.matched, field.mismatched, field.missing) == (1, 1, 1)
    assert score.accuracy == 1 / 3
    assert field.coverage == 2 / 3


def test_score_extraction_caps_mismatch_evidence_but_not_the_counts() -> None:
    samples = [_sample(f"s{i}", {"x": "expected"}, {"x": f"other{i}"}) for i in range(30)]

    score = score_extraction(samples, ["x"], max_mismatches=5)

    assert len(score.mismatches) == 5
    assert score.fields["x"].mismatched == 30


def test_score_extraction_counts_samples_and_errors() -> None:
    samples = [
        _sample("a", {"x": "1"}, {"x": "1"}),
        _sample("b", {"x": "2"}, None, error="crash"),
    ]

    score = score_extraction(samples, ["x"])

    assert score.samples_total == 2
    assert score.samples_with_error == 1
    assert score.samples_scored == 2


def test_accuracy_is_none_when_nothing_was_judgeable() -> None:
    score = score_extraction([_sample("a", {"x": None}, {"x": "ignored"})], ["x"])

    assert score.accuracy is None
    assert score.coverage is None
    assert "NO JUDGEABLE" in format_score(score, strategy="table_label")


def test_format_score_reports_strategy_policy_and_worst_mismatches() -> None:
    score = score_extraction(
        [_sample("a", {"case_number": "2023/111"}, {"case_number": "2023/999"})], ["case_number"], policy=MatchPolicy.FUZZY
    )

    report = format_score(score, strategy="table_label")

    assert "strategy: table_label" in report
    assert "policy: fuzzy @ 85" in report
    assert "accuracy: 0.0%" in report
    assert "2023/111 -> 2023/999" in report


def test_fuzzy_cutoff_is_reported_by_its_value() -> None:
    score = score_extraction([], ["x"], policy=MatchPolicy.FUZZY, fuzzy_cutoff=92.5)

    assert score.fuzzy_cutoff == 92.5
