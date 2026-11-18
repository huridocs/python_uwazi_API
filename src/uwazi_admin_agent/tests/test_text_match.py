"""Isolated unit tests for the ``textmatch`` sandbox namespace (per AGENTS.md).

Pure scoring over rapidfuzz with literal inputs: no mocks, no network, no model
download. These pin the contract a generated script codes against — scores in
0..100, ``rank`` preserving the input index, and a cutoff that REFUSES a weak
match instead of silently returning the closest label (a wrong thesaurus label
would be written into Uwazi).
"""

from __future__ import annotations

from uwazi_admin_agent.domain.text_match import (
    SCORE_MAX,
    SCORE_MIN,
    Scorer,
    best,
    partial_ratio,
    rank,
    ratio,
    token_set_ratio,
    wratio,
)


def test_scores_are_bounded_0_to_100() -> None:
    for value in (ratio("abc", "abc"), wratio("abc", "xyz"), partial_ratio("abc", "xxabcxx")):
        assert SCORE_MIN <= value <= SCORE_MAX


def test_ratio_is_identity_for_equal_strings() -> None:
    assert ratio("Case number", "Case number") == 100.0


def test_wratio_matches_label_spelling_variants_better_than_ratio() -> None:
    """The label-matching case the namespace exists for: a document header
    "Case No." against the schema label "case number" must score high enough to
    clear a threshold, where raw edit distance alone is diluted."""
    assert wratio("Case No.", "case number") > ratio("Case No.", "case number")


def test_token_set_ratio_ignores_token_order_and_extra_tokens() -> None:
    assert token_set_ratio("date of judgment", "Judgment Date") == 100.0


def test_partial_ratio_finds_a_label_inside_a_long_paragraph() -> None:
    paragraph = "The court, having considered the case number 2023/111, hereby rules."
    assert partial_ratio("case number", paragraph) >= 90.0


def test_rank_orders_by_score_and_maps_back_to_the_input_index() -> None:
    choices = ["Claim", "Case No.", "CASE NUMBER"]

    hits = rank("case number", choices)

    assert [hit["index"] for hit in hits[:2]] == [2, 1]
    assert hits[0]["choice"] == "CASE NUMBER"
    assert hits[0]["score"] >= hits[1]["score"]


def test_scorers_normalise_case_and_punctuation() -> None:
    """Without normalisation "Case No." vs "case number" scores 42 while
    "CASE NUMBER" vs "case number" scores 9 — the opposite of what a label matcher
    must do. The processor is applied inside every scorer."""
    assert wratio("Case No.", "case number") >= 80.0
    assert wratio("CASE NUMBER", "case number") == 100.0
    assert wratio("DATE OF JUDGMENT", "judgment date") >= 90.0


def test_rank_keeps_every_choice_above_the_cutoff_only() -> None:
    hits = rank("case number", ["Case No.", "Claim"], score_cutoff=80.0)

    assert [hit["choice"] for hit in hits] == ["Case No."]


def test_rank_honours_limit_after_sorting() -> None:
    hits = rank("abc", ["abc", "abd", "abe"], limit=1)

    assert len(hits) == 1
    assert hits[0]["choice"] == "abc"


def test_rank_empty_choices_or_zero_limit_returns_empty() -> None:
    assert rank("x", []) == []
    assert rank("x", ["x"], limit=0) == []


def test_rank_ties_keep_input_order() -> None:
    hits = rank("same", ["same", "same"])

    assert [hit["index"] for hit in hits] == [0, 1]


def test_best_returns_none_below_cutoff_never_a_guess() -> None:
    """A classification that matched nothing must be reported as nothing: writing
    the closest thesaurus label regardless would be a wrong value."""
    assert best("completely unrelated topic", ["Contract", "Judgment"], score_cutoff=90.0) is None


def test_best_returns_the_single_hit_dict() -> None:
    hit = best("Case No.", ["Claim", "Case number"], score_cutoff=60.0)

    assert hit is not None
    assert hit["choice"] == "Case number"
    assert set(hit) == {"choice", "score", "index"}


def test_none_and_numbers_are_coerced_not_raised() -> None:
    """Script values come from entity dicts: a missing value (None) or a numeric
    property must not crash a per-entity pass."""
    assert ratio(None, "") == 100.0
    assert ratio(2023, "2023") == 100.0


def test_scorer_accepts_its_name_as_a_string() -> None:
    assert wratio("a", "a") == best("a", ["a"], scorer=Scorer.WRATIO)["score"]


def test_textmatch_is_bound_in_the_script_sandbox() -> None:
    """The generated script calls `textmatch.*` by bare name; the namespace must
    carry every documented member (no import is possible)."""
    from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB

    textmatch = _STDLIB["textmatch"]
    for member in ("ratio", "partial_ratio", "token_sort_ratio", "token_set_ratio", "wratio", "rank", "best"):
        assert callable(getattr(textmatch, member)), member


def test_textmatch_runs_inside_the_script_sandbox_namespace() -> None:
    """End-to-end in the real sandbox: the classification idiom runs with ZERO
    imports and refuses a non-matching topic."""
    from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS, run_script_sync

    code = """
labels = ["Contract", "Judgment"]
hit = textmatch.best("judgment of the court", labels, score_cutoff=80)
miss = textmatch.best("quarterly budget", labels, score_cutoff=80)
result = f"{hit['choice']}|{'None' if miss is None else 'BUG'}"
"""
    result, error = run_script_sync(code, {"__builtins__": SAFE_BUILTINS, **_STDLIB})

    assert error is None
    assert result == "Judgment|None"
