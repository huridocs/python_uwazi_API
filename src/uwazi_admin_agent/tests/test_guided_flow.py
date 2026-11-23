"""Isolated tests for the guided wizard's conditional question navigation."""

from uwazi_admin_agent.domain.guided_flow import guided_question_ids, next_question_id, question_error
from uwazi_admin_agent.domain.guided_task import (
    EXISTING_CUSTOM,
    FALLBACK_VALUE,
    GOAL_CLASSIFY,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    OUTPUT_SPECIFIC,
    SCOPE_FILTER,
    SOURCE_METADATA,
    SOURCE_PDF,
    GuidedAnswers,
)


def test_fill_flow_only_shows_the_custom_existing_value_follow_up_when_selected() -> None:
    answers = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,))
    assert "existing_value_rule" not in guided_question_ids(answers)
    assert next_question_id("existing_value_mode", answers) == "scope"

    custom = GuidedAnswers(**{**answers.__dict__, "existing_value_mode": EXISTING_CUSTOM})
    assert next_question_id("existing_value_mode", custom) == "existing_value_rule"
    assert next_question_id("existing_value_rule", custom) == "scope"


def test_conditional_follow_ups_appear_only_for_answers_that_need_them() -> None:
    answers = GuidedAnswers(
        goal=GOAL_CLASSIFY,
        fallback_mode=FALLBACK_VALUE,
        entity_scope=SCOPE_FILTER,
        sources=(SOURCE_METADATA,),
        input_language_mode=INPUT_LANGUAGE_SPECIFIC,
        output_language_mode=OUTPUT_SPECIFIC,
    )
    questions = guided_question_ids(answers)
    assert questions.index("fallback_value") == questions.index("fallback_mode") + 1
    assert questions.index("scope_filter") == questions.index("scope") + 1
    assert questions.index("metadata_properties") == questions.index("sources") + 1
    assert questions.index("input_languages") == questions.index("input_language_mode") + 1
    assert questions.index("output_languages") == questions.index("output_language_mode") + 1
    assert next_question_id("examples", answers) == "review"


def test_custom_task_skips_target_property_and_fill_or_classify_rules() -> None:
    questions = guided_question_ids(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,)))
    assert questions[:3] == ("templates", "custom_description", "scope")
    assert "target_property" not in questions
    assert "fill_rules" not in questions
    assert "fallback_mode" not in questions


def test_visible_question_validation_blocks_incomplete_required_answers() -> None:
    answers = GuidedAnswers()
    assert question_error("target_property", answers) is not None
    assert question_error("sources", answers) is not None
    assert question_error("document_guidance", answers) is None

    complete = GuidedAnswers(**{**answers.__dict__, "target_property": "topics", "sources": (SOURCE_PDF,)})
    assert question_error("target_property", complete) is None
    assert question_error("sources", complete) is None
