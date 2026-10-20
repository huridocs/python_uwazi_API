"""Isolated unit tests for the text-property e2e driver's pure helpers.

Per ``AGENTS.md`` these are isolated unit tests: no mocks, no network, no
real instance. They cover the parts of the driver that DECIDE something (the
value reader, the prompt builder, the report aggregation) — the parts whose
bug would silently produce a wrong verdict.

The live end-to-end exercise lives in
``uwazi_admin_agent.drivers.step_text_property_e2e`` and is run against a real
instance (``uwazi-admin-agent text-property-e2e``); these tests only pin the
pure seams.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.drivers.step_text_property_e2e import (
    Check,
    StepReport,
    TaskReport,
    _comparable,
    _field_diff,
    _metadata_diff,
    _prompt_both_languages,
    _prompt_mixed_batches,
    _prompt_single_language,
    _text_value,
)

# --- the value reader ---------------------------------------------------------


def test_a_text_property_reads_as_the_string_uwazi_holds() -> None:
    """The stored shape is a list of ``{"value": …}`` entries."""
    raw = {"metadata": {"document_ref": [{"value": "A/HRC/37/NGO/88"}]}}

    assert _text_value(raw, "document_ref") == "A/HRC/37/NGO/88"


def test_an_unset_property_reads_as_none() -> None:
    """An empty cell means "the row holds no value" — not an empty string."""
    assert _text_value({"metadata": {"document_ref": []}}, "document_ref") is None
    assert _text_value({"metadata": {}}, "document_ref") is None


def test_a_missing_metadata_key_reads_as_none() -> None:
    assert _text_value({}, "document_ref") is None


def test_a_bare_string_cell_reads_as_itself() -> None:
    """Defensive: a non-envelope cell still reads, it does not crash."""
    assert _text_value({"metadata": {"document_ref": "plain"}}, "document_ref") == "plain"


def test_a_non_string_value_is_stable_json() -> None:
    """A non-string value is rendered deterministically, so a comparison is stable."""
    raw = {"metadata": {"document_ref": [{"value": {"a": 1, "b": 2}}]}}

    assert _text_value(raw, "document_ref") == '{"a": 1, "b": 2}'


def test_the_reader_never_reads_another_property() -> None:
    """The reader is property-scoped: one property's value is not another's."""
    raw = {"metadata": {"document_ref": [{"value": "REF"}], "summary": [{"value": "SUM"}]}}

    assert _text_value(raw, "document_ref") == "REF"
    assert _text_value(raw, "summary") == "SUM"


# --- the prompts --------------------------------------------------------------


def test_the_single_language_prompt_names_the_language_and_the_entities() -> None:
    """The prompt is the ONLY thing telling the generator which rows to write."""
    prompt = _prompt_single_language("DOCUMENT", "document_ref", ["aaa", "bbb"], "es", "VALUE-ES")

    assert "'es' language only" in prompt
    assert "aaa, bbb" in prompt
    assert "VALUE-ES" in prompt
    assert "document_ref" in prompt
    assert "DOCUMENT" in prompt


def test_the_both_languages_prompt_names_both_values() -> None:
    """A run writing both rows must name BOTH values, or one is guessed."""
    prompt = _prompt_both_languages("DOCUMENT", "document_ref", ["aaa"], "VALUE-EN", "VALUE-ES")

    assert "VALUE-EN" in prompt
    assert "VALUE-ES" in prompt
    assert "must not be merged" in prompt


def test_the_mixed_batch_prompt_names_both_entity_sets() -> None:
    """The two language groups must be distinguishable in the prompt."""
    prompt = _prompt_mixed_batches("DOCUMENT", "document_ref", ["aaa", "bbb"], ["ccc"], "VALUE-EN", "VALUE-ES")

    assert "aaa, bbb" in prompt
    assert "ccc" in prompt
    assert "VALUE-EN" in prompt
    assert "VALUE-ES" in prompt


def test_the_prompts_never_ask_for_a_property_the_template_lacks() -> None:
    """A prompt names the property; the driver resolves it from the template."""
    prompt = _prompt_single_language("DOCUMENT", "document_ref", ["aaa"], "en", "V")

    assert "title" not in prompt


# --- the report ---------------------------------------------------------------


def test_a_report_with_only_passing_checks_is_ok() -> None:
    report = TaskReport(task_id="t", name="n")
    report.add("a", True)

    assert report.ok


def test_a_report_with_a_failing_check_is_not_ok() -> None:
    report = TaskReport(task_id="t", name="n")
    report.add("a", True)
    report.add("b", False)

    assert not report.ok


def test_a_report_with_an_error_is_not_ok() -> None:
    """An errored task never reads as OK, whatever its checks say."""
    report = TaskReport(task_id="t", name="n")
    report.add("a", True)
    report.error = "generation failed"

    assert not report.ok


def test_a_step_with_an_error_is_not_ok() -> None:
    step = StepReport(title="t", run_id="r", prompt="p")
    step.add("a", True)
    step.error = "run failed"

    assert not step.ok


@pytest.mark.parametrize(
    ("ok", "expected"),
    [(True, "PASS"), (False, "FAIL")],
)
def test_a_check_line_carries_its_verdict(ok: bool, expected: str) -> None:
    line = Check(name="the thing", ok=ok).line()

    assert line.startswith(f"  [{expected}] the thing")


def test_a_check_line_names_the_detail_when_there_is_one() -> None:
    line = Check(name="the thing", ok=False, detail="expected 'X'").line()

    assert "expected 'X'" in line


# --- data preservation: what a comparison can and cannot judge ----------------


def test_edit_date_is_excluded_from_the_comparison() -> None:
    """``editDate`` advances on every save — including the revert's own save —
    so a difference there is not a data difference."""
    raw = {"_id": "x", "title": "T", "editDate": 1, "metadata": {}}

    assert "editDate" not in _comparable(raw)
    assert _comparable(raw)["title"] == "T"


def test_relations_are_excluded_from_the_comparison() -> None:
    """``relations`` is a read-only denormalized view re-derived on every read."""
    raw = {"_id": "x", "relations": [{"entity": "y"}], "metadata": {}}

    assert "relations" not in _comparable(raw)


def test_the_comparison_keeps_every_other_field() -> None:
    """Only the two server-managed views are dropped — the data is all kept."""
    raw = {
        "_id": "x",
        "sharedId": "s",
        "title": "T",
        "template": "tmpl",
        "documents": [{"filename": "f.pdf"}],
        "attachments": [],
        "permissions": [{"refId": "public"}],
        "metadata": {"document_ref": [{"value": "R"}]},
    }

    assert set(_comparable(raw)) == set(raw)


def test_the_comparison_of_an_absent_raw_is_empty() -> None:
    assert _comparable(None) == {}


def test_the_metadata_diff_names_the_changed_properties() -> None:
    """The diff is per PROPERTY, so a check can name what was lost."""
    before = {"metadata": {"document_ref": [{"value": "A"}], "document_type": [{"value": "T"}]}}
    after = {"metadata": {"document_ref": [{"value": "B"}], "document_type": [{"value": "T"}]}}

    assert _metadata_diff(before, after) == ["document_ref"]


def test_the_metadata_diff_is_empty_for_two_identical_entities() -> None:
    before = {"metadata": {"document_ref": [{"value": "A"}]}}
    after = {"metadata": {"document_ref": [{"value": "A"}]}}

    assert _metadata_diff(before, after) == []


def test_the_metadata_diff_sees_a_property_that_was_cleared() -> None:
    """A property that LOST its value is a difference — the empty list is not
    the same as the missing key."""
    before = {"metadata": {"summary": [{"value": "text"}]}}
    after = {"metadata": {"summary": []}}

    assert _metadata_diff(before, after) == ["summary"]


def test_the_metadata_diff_of_entities_without_metadata_is_empty() -> None:
    assert _metadata_diff({}, {}) == []


def test_the_field_diff_covers_the_non_metadata_fields() -> None:
    """The title (and any other top-level field) is compared too."""
    before = {"title": "Old", "template": "tmpl", "metadata": {}}
    after = {"title": "New", "template": "tmpl", "metadata": {}}

    assert _field_diff(before, after) == ["title"]


def test_the_field_diff_excludes_the_metadata_itself() -> None:
    """The metadata is compared property by property, not as one blob."""
    before = {"title": "T", "metadata": {"p": [{"value": "A"}]}}
    after = {"title": "T", "metadata": {"p": [{"value": "B"}]}}

    assert _field_diff(before, after) == []


def test_the_field_diff_sees_a_lost_file() -> None:
    """A dropped document/attachment is a field difference."""
    before = {"documents": [{"filename": "f.pdf"}], "metadata": {}}
    after = {"documents": [], "metadata": {}}

    assert _field_diff(before, after) == ["documents"]


def test_the_field_diff_of_two_identical_entities_is_empty() -> None:
    raw = {"title": "T", "metadata": {"p": [{"value": "A"}]}}

    assert _field_diff(raw, dict(raw)) == []
