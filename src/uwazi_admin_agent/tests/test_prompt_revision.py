"""Isolated unit tests for the prompt-revision seam (per AGENTS.md).

Pure functions over literal inputs: no mocks, no network, no env credentials.
The prompt chat's LLM-backed turn is not unit-tested; the text it records —
what the generation LLM receives when the operator flags a wrong suggestion —
is.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.prompt_revision import (
    CORRECTION_HEADING,
    PREVIOUS_SCRIPT_HEADING,
    SuggestionCorrection,
    correction_request,
    correction_summary,
    prompt_with_previous_script,
    property_line,
)
from uwazi_admin_agent.domain.suggestion import Suggestion
from uwazi_admin_agent.drivers.web.review_page.entity_window import _suggestion_correction

CORRECTION = SuggestionCorrection(
    shared_id="abc123",
    entity_title="Case 7 — Land dispute",
    template_name="Decision",
    language="en",
    property_name="document_ref",
    property_label="Document Ref",
    current_value="— (empty)",
    proposed_value="2020-01-01",
    evidence="page 1: 'Ref. 2020-01-01'",
    reason="That date is the filing date, not the document reference.",
)


def test_property_line_prefers_the_label_and_keeps_the_raw_name() -> None:
    assert property_line(CORRECTION) == "Document Ref (document_ref)"


def test_property_line_falls_back_to_the_raw_name() -> None:
    raw_only = CORRECTION.model_copy(update={"property_label": "document_ref"})
    assert property_line(raw_only) == "document_ref"
    unnamed = CORRECTION.model_copy(update={"property_label": None, "property_name": None})
    assert property_line(unnamed) == "(whole entity)"


def test_correction_request_carries_the_row_and_the_operator_explanation() -> None:
    request = correction_request(CORRECTION)

    assert request.startswith(CORRECTION_HEADING)
    assert "Case 7 — Land dispute (abc123)" in request
    assert "Template: Decision" in request
    assert "Language: en" in request
    assert "Property: Document Ref (document_ref)" in request
    assert "Current value in Uwazi:\n— (empty)" in request
    assert "Value the current script proposed (incorrect):\n2020-01-01" in request
    assert "Evidence the script recorded for this row:\npage 1: 'Ref. 2020-01-01'" in request
    assert "Why this suggestion is wrong:\nThat date is the filing date" in request
    assert "do NOT special-case this entity's sharedId" in request


def test_correction_request_without_an_explanation_says_so() -> None:
    request = correction_request(CORRECTION.model_copy(update={"reason": "  "}))
    assert "the operator did not explain it" in request


def test_correction_request_omits_the_evidence_block_when_there_is_none() -> None:
    request = correction_request(CORRECTION.model_copy(update={"evidence": ""}))
    assert "Evidence the script recorded for this row" not in request


def test_correction_summary_is_the_attachment_shown_above_the_chat() -> None:
    summary = correction_summary(CORRECTION)
    assert summary.splitlines() == [
        "Case 7 — Land dispute — abc123",
        "Template: Decision",
        "Property: Document Ref (document_ref) · en",
        "Current value in Uwazi: — (empty)",
        "Proposed value (wrong): 2020-01-01",
    ]


def test_correction_summary_falls_back_to_the_shared_id_as_title() -> None:
    summary = correction_summary(CORRECTION.model_copy(update={"entity_title": ""}))
    assert summary.startswith("abc123 — abc123")


def test_prompt_with_previous_script_sends_the_prompt_first_and_the_script_after() -> None:
    script = "def run(ctx):\n    ctx.update(...)\n"
    combined = prompt_with_previous_script("Fix the extraction.", script)

    assert combined.startswith("Fix the extraction.")
    assert PREVIOUS_SCRIPT_HEADING in combined
    assert "```python\ndef run(ctx):\n    ctx.update(...)\n```" in combined
    assert combined.index("Fix the extraction.") < combined.index("def run(ctx):")


def test_prompt_with_previous_script_without_a_script_changes_nothing() -> None:
    assert prompt_with_previous_script("Fix the extraction.", "  \n") == "Fix the extraction."


def test_the_entity_window_builds_the_correction_from_a_review_row() -> None:
    suggestion = Suggestion(
        run_id="run-1",
        shared_id="abc123",
        entity_title="Case 7",
        template_name="Decision",
        op="update",
        language="en",
        property_name="document_ref",
        proposed_value=[{"value": "2020-01-01", "language": "en"}],
        current_value=[{"value": "", "language": "en"}],
        context="page 1: 'Ref. 2020-01-01'",
    )

    correction = _suggestion_correction(suggestion, {"document_ref": "Document Ref"}, {})

    assert correction.shared_id == "abc123"
    assert correction.property_label == "Document Ref"
    assert correction.current_value == ""
    assert correction.proposed_value == "2020-01-01"
    assert correction.evidence == "page 1: 'Ref. 2020-01-01'"
    assert "Document Ref (document_ref)" in correction_request(correction)


def test_the_entity_window_uses_the_skip_reason_as_evidence() -> None:
    suggestion = Suggestion(
        run_id="run-1",
        shared_id="abc123",
        op="skipped",
        property_name=None,
        skip_reason="no_document",
        proposed_value="No primary document attached.",
    )

    correction = _suggestion_correction(suggestion, {}, {})

    assert correction.evidence == "No primary document is attached to this entity."
