"""Offline unit tests for the test-set note folded into the AI check (pure)."""

from uwazi_admin_agent.domain.validation import (
    VALIDATION_AUTOMATIC,
    VALIDATION_CUSTOM,
    VALIDATION_NONE,
    ValidationConfig,
)
from uwazi_admin_agent.domain.validation_note import validation_note_to_user_block


def test_no_note_when_no_test_set_is_kept_or_no_property_is_measured() -> None:
    assert validation_note_to_user_block(None) == ""
    assert validation_note_to_user_block(ValidationConfig(mode=VALIDATION_NONE)) == ""
    assert validation_note_to_user_block(ValidationConfig(mode=VALIDATION_AUTOMATIC)) == ""


def test_automatic_note_names_the_property_type_sample_and_templates() -> None:
    note = validation_note_to_user_block(
        ValidationConfig(
            mode=VALIDATION_AUTOMATIC,
            property_name="topics",
            property_type="multiselect",
            template_names=["Judgment"],
            sample_size=25,
        )
    )

    assert note.startswith("\n\n--- OPERATOR-CHOSEN TEST SET ---")
    assert note.endswith("--- END TEST SET ---")
    assert "topics (multiselect)" in note
    assert "up to 25 entities" in note
    assert "Judgment" in note
    assert "held-out" in note.lower()


def test_custom_note_carries_the_selection_rule_the_operator_described() -> None:
    note = validation_note_to_user_block(
        ValidationConfig(
            mode=VALIDATION_CUSTOM,
            property_name="title",
            sample_size=10,
            selector_explanation="judgments from Kigali dated 2024",
            selector_code="result = []",
        )
    )

    assert "judgments from Kigali dated 2024" in note
    assert "title" in note


def test_a_custom_set_without_a_description_still_names_the_rule() -> None:
    note = validation_note_to_user_block(ValidationConfig(mode=VALIDATION_CUSTOM, property_name="title", sample_size=10))

    assert "a custom rule" in note


def test_note_without_templates_falls_back_to_the_task_targets() -> None:
    note = validation_note_to_user_block(ValidationConfig(mode=VALIDATION_AUTOMATIC, property_name="judge", sample_size=5))

    assert "the templates the task targets" in note
