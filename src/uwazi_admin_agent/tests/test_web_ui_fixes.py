"""Isolated unit tests for the web-UI fixes' pure seams (per AGENTS.md).

Literal inputs, plain assertions, no mocks, no network, no NiceGUI rendering.
Covers: the task CSV export, the multi-correction prompt request, the live
generation stage labels, and the wizard's guaranteed Title property.
"""

from __future__ import annotations

import csv
import io

from uwazi_admin_agent.domain.prompt_revision import (
    SuggestionCorrection,
    combined_correction_request,
    correction_request,
    merge_corrections,
    prompt_with_previous_script,
)
from uwazi_admin_agent.domain.task_export import EXPORT_COLUMNS, tasks_csv
from uwazi_admin_agent.drivers.web.operation_gate import (
    RunClaim,
    release_run,
    run_claim,
    set_claim_note,
    try_claim_run,
)
from uwazi_admin_agent.drivers.web.review_view import OPERATION_SORT_OPTIONS
from uwazi_admin_agent.drivers.web.run_service.wizard_schema import wizard_schema_from_models
from uwazi_admin_agent.use_cases.generation_stages import (
    STAGE_PLANNING,
    STAGE_THINKING,
    benchmark_stage,
    tool_stage,
)
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template


def _correction(shared_id: str, **overrides: object) -> SuggestionCorrection:
    values: dict[str, object] = {
        "shared_id": shared_id,
        "entity_title": f"Case {shared_id}",
        "property_name": "document_ref",
        "property_label": "Document Ref",
        "current_value": "— (empty)",
        "proposed_value": "wrong",
    }
    values.update(overrides)
    return SuggestionCorrection(**values)


# --- task export --------------------------------------------------------------


def test_tasks_csv_quotes_a_multiline_prompt_into_one_cell() -> None:
    text = tasks_csv([{"run_id": "run-1", "status": "planned", "prompt": "line one\nline two", "modified": 3}])

    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 1
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["prompt"] == "line one\nline two"
    assert rows[0]["modified"] == "3"
    # Missing keys are empty cells, never a shifted row.
    assert rows[0]["validated_prompt"] == ""


def test_tasks_csv_header_is_the_export_contract() -> None:
    header = tasks_csv([]).strip().split(",")
    assert header == [f'"{column}"' for column in EXPORT_COLUMNS]
    assert "prompt" in EXPORT_COLUMNS and "validated_prompt" in EXPORT_COLUMNS


def test_tasks_csv_renders_datetimes_as_iso_and_none_as_empty() -> None:
    from datetime import datetime, timezone

    text = tasks_csv([{"run_id": "r", "created_at": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc), "error": None}])
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["created_at"].startswith("2026-01-02T03:04")
    assert row["error"] == ""


# --- multiple corrections -----------------------------------------------------


def test_one_correction_keeps_the_single_request_text() -> None:
    correction = _correction("a1")
    assert combined_correction_request([correction]) == correction_request(correction)


def test_several_corrections_are_numbered_in_one_request() -> None:
    request = combined_correction_request([_correction("a1"), _correction("a2"), _correction("a3")])

    assert "(3 rows)" in request
    assert "Fix 1 of 3" in request and "Fix 3 of 3" in request
    assert request.count("Entity:") == 3
    # Each row's evidence is present, and the shared instruction is issued ONCE.
    assert "Case a1 (a1)" in request and "Case a3 (a3)" in request
    assert request.count("do NOT special-case") == 1


def test_the_correction_instruction_keeps_the_first_generation_tools_available() -> None:
    request = correction_request(_correction("a1"))
    assert "benchmark_extractor" in request
    assert "author_html_extractor" in request


def test_merge_corrections_replaces_a_row_flagged_twice_in_place() -> None:
    first = _correction("a1")
    second = _correction("a2")
    merged = merge_corrections([first, second], first.model_copy(update={"reason": "the filing date"}))

    assert [item.shared_id for item in merged] == ["a1", "a2"]
    assert merged[0].reason == "the filing date"
    assert merged[1].reason == ""


def test_merge_corrections_appends_a_new_row() -> None:
    merged = merge_corrections([_correction("a1")], _correction("a2"))
    assert [item.shared_id for item in merged] == ["a1", "a2"]


def test_previous_script_attachment_asks_for_the_same_authoring_path() -> None:
    combined = prompt_with_previous_script("Fix the extraction.", "def run():\n    pass\n")
    assert "benchmark_extractor" in combined
    assert "better than this one" in combined


# --- live generation stages ---------------------------------------------------


def test_known_tools_report_specific_stages_and_unknown_ones_fall_back() -> None:
    assert tool_stage("benchmark_extractor") == "Running the extraction benchmark…"
    assert tool_stage("author_html_extractor").startswith("LLM is authoring")
    assert tool_stage("something_new") == STAGE_THINKING
    assert STAGE_PLANNING.startswith("Running the generated script")


def test_the_benchmark_stage_numbers_repeated_rounds() -> None:
    assert benchmark_stage(1) == "Running the extraction benchmark…"
    assert "round 2" in benchmark_stage(2)


def test_a_claim_publishes_its_stage_and_drops_it_with_the_claim() -> None:
    instance = "http://stage-gate.test"
    try:
        assert try_claim_run(instance, "run-1", "creating") is True
        set_claim_note(instance, "run-1", "Running the extraction benchmark…")
        claim: RunClaim | None = run_claim(instance, "run-1")
        assert claim is not None and claim.note == "Running the extraction benchmark…"
    finally:
        release_run(instance, "run-1", "creating")
    assert run_claim(instance, "run-1") is None


def test_a_stage_for_an_unclaimed_run_is_ignored() -> None:
    instance = "http://stage-gate-orphan.test"
    set_claim_note(instance, "never-claimed", "stale")
    assert run_claim(instance, "never-claimed") is None


# --- review labels ------------------------------------------------------------


def test_the_sort_label_reads_as_plain_english() -> None:
    assert OPERATION_SORT_OPTIONS["recorded"] == "Original order"
    assert OPERATION_SORT_OPTIONS["ops"] == "Operations"


# --- wizard: the title is always offered --------------------------------------


def test_wizard_schema_keeps_title_even_without_common_properties() -> None:
    template = Template.model_construct(
        name="Judgment",
        properties=[PropertySchema(name="judge", label="Judge", type=PropertyType.TEXT)],
        common_properties=[],
    )
    schema = wizard_schema_from_models([template], [])
    names = [prop.name for prop in schema.templates[0].properties]
    assert names[0] == "title"
    assert "judge" in names
