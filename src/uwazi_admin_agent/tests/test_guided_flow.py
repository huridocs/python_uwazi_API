"""Isolated tests for the guided wizard's conditional question navigation."""

from uwazi_admin_agent.domain.guided_flow import guided_question_ids, next_question_id, question_error
from uwazi_admin_agent.domain.guided_task import (
    CREATE_SOURCE_DOCUMENTS,
    CREATE_SOURCE_EXISTING,
    CREATE_SOURCE_UNRELATED,
    FALLBACK_VALUE,
    GOAL_CREATE,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    INPUT_LANGUAGE_SPECIFIC_FALLBACK,
    OUTPUT_SPECIFIC,
    SCOPE_FILTER,
    SOURCE_METADATA,
    SOURCE_PDF,
    GuidedAnswers,
    assemble_prompt,
)


def test_fill_flow_never_asks_about_the_current_value_of_the_property() -> None:
    """Keeping/replacing an existing value is a per-row review decision, and a
    suggestion must be computed from the task's input — so neither the question
    nor its custom-rule follow-up is ever offered."""
    extracting = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), target_property_type="text")
    questions = guided_question_ids(extracting)
    assert "existing_value_mode" not in questions
    assert "existing_value_rule" not in questions
    assert next_question_id("target_property", extracting) == "scope"
    assert next_question_id("generation", extracting) == "review"


def test_fill_target_type_selects_classification_or_extraction_branch() -> None:
    classifying = GuidedAnswers(goal=GOAL_FILL, target_property="topics", target_property_type="multiselect")
    extracting = GuidedAnswers(goal=GOAL_FILL, target_property="judge", target_property_type="text")

    classify_questions = guided_question_ids(classifying)
    assert "fallback_mode" in classify_questions
    assert "existing_value_mode" not in classify_questions
    # A classification target needs no extraction rules.
    assert question_error("generation", classifying) is None

    extract_questions = guided_question_ids(extracting)
    assert "fallback_mode" not in extract_questions
    assert "existing_value_mode" not in extract_questions
    assert question_error("generation", extracting) is not None


def test_conditional_follow_ups_appear_only_for_answers_that_need_them() -> None:
    answers = GuidedAnswers(
        goal=GOAL_FILL,
        target_property="topics",
        target_property_type="multiselect",
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
    # A multiselect target stores ONE shared value (no language row), so no
    # entity-language question follows; that question exists for per-language
    # text targets only (covered separately).
    assert "output_language_mode" not in questions
    assert "output_languages" not in questions
    assert questions.index("generation") == len(questions) - 2
    assert next_question_id("generation", answers) == "review"


def test_primary_language_with_fallback_also_asks_which_language() -> None:
    answers = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), input_language_mode=INPUT_LANGUAGE_SPECIFIC_FALLBACK)
    questions = guided_question_ids(answers)
    assert questions.index("input_languages") == questions.index("input_language_mode") + 1
    assert question_error("input_languages", answers) is not None


def test_create_flow_keeps_the_specific_output_languages_question() -> None:
    answers = GuidedAnswers(
        goal=GOAL_CREATE,
        create_source=CREATE_SOURCE_DOCUMENTS,
        output_language_mode=OUTPUT_SPECIFIC,
    )
    questions = guided_question_ids(answers)
    assert questions.index("output_languages") == questions.index("output_language_mode") + 1
    all_mode = GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_DOCUMENTS)
    # The "all configured languages" (translation-tool) mode is not offered:
    # the create flow's choice collapses to source-language by default.
    assert "output_languages" not in guided_question_ids(all_mode)


def test_fill_text_target_asks_which_entity_language_is_updated() -> None:
    """A per-language text target is written into a language ROW, so the
    operator must say which one — even when the document's language differs."""
    extracting = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), target_property_type="text")
    questions = guided_question_ids(extracting)
    assert "output_language_mode" in questions
    assert questions.index("output_language_mode") > questions.index("input_language_mode")
    # A shared (non-per-language) property has no row to pick.
    shared = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), target_property_type="multiselect")
    assert "output_language_mode" not in guided_question_ids(shared)


def test_fill_prompt_keeps_input_and_output_languages_independent() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_FILL,
            sources=(SOURCE_PDF,),
            target_property="judge",
            target_property_type="text",
            fill_rules="One name only.",
            input_language_mode=INPUT_LANGUAGE_SPECIFIC_FALLBACK,
            input_languages=("es",),
            output_language_mode=OUTPUT_SPECIFIC,
            output_languages=("en",),
        ),
        {"en": "English", "es": "Spanish"},
    )
    assert "Prefer reading from: Spanish (es)." in prompt
    assert "Write the suggestion only into the entity's language(s): English (en)." in prompt
    assert "may update the entity's English fields" in prompt


def test_guided_flow_asks_output_language_questions_for_per_language_fill_targets() -> None:
    """Only a per-language text target needs the entity-language question; a
    multiselect (shared value) target does not."""
    text_target = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), target_property_type="text")
    assert "output_language_mode" in guided_question_ids(text_target)
    shared_target = GuidedAnswers(goal=GOAL_FILL, sources=(SOURCE_PDF,), target_property_type="multiselect")
    assert "output_language_mode" not in guided_question_ids(shared_target)
    # A custom task has no declared target: the wizard asks, so the operator can
    # still say which entity row the task must update.
    assert "output_language_mode" in guided_question_ids(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,)))


def test_custom_task_skips_target_property_and_fill_or_classify_rules() -> None:
    questions = guided_question_ids(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,)))
    assert questions[:3] == ("templates", "custom_description", "scope")
    assert "target_property" not in questions
    assert "fill_rules" not in questions
    assert "fallback_mode" not in questions


def test_create_from_existing_properties_asks_for_source_and_target_templates() -> None:
    answers = GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_EXISTING)
    questions = guided_question_ids(answers)
    assert questions[:3] == ("templates", "create_source", "source_templates")
    assert "create_source_properties" in questions
    assert "create_target_properties" in questions
    assert "sources" not in questions
    assert "create_count" not in questions


def test_create_from_documents_drops_the_source_property_question() -> None:
    questions = guided_question_ids(GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_DOCUMENTS))
    assert "source_templates" in questions
    assert "create_source_properties" not in questions
    assert "create_target_properties" in questions


def test_create_unrelated_asks_count_and_instructions_without_scope() -> None:
    questions = guided_question_ids(GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_UNRELATED))
    assert questions[:3] == ("templates", "create_source", "create_count")
    assert "create_instructions" in questions
    assert "scope" not in questions
    assert "source_templates" not in questions
    assert "input_language_mode" not in questions
    assert "document_guidance" not in questions


def test_visible_question_validation_blocks_incomplete_required_answers() -> None:
    answers = GuidedAnswers()
    assert question_error("target_property", answers) is not None
    assert question_error("sources", answers) is not None
    assert question_error("document_guidance", answers) is None

    complete = GuidedAnswers(**{**answers.__dict__, "target_property": "topics", "sources": (SOURCE_PDF,)})
    assert question_error("target_property", complete) is None
    assert question_error("sources", complete) is None


def test_create_question_validation_blocks_incomplete_required_answers() -> None:
    empty = GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_UNRELATED)
    assert question_error("templates", empty) is not None
    assert question_error("create_count", empty) is not None
    assert question_error("create_instructions", empty) is not None
    assert question_error("create_target_properties", empty) is not None

    complete = GuidedAnswers(
        goal=GOAL_CREATE,
        create_source=CREATE_SOURCE_UNRELATED,
        templates=("Decision",),
        create_count="10",
        create_instructions="Fictional rulings",
        create_target_properties=("title",),
    )
    assert question_error("templates", complete) is None
    assert question_error("create_count", complete) is None
    assert question_error("create_instructions", complete) is None
    assert question_error("create_target_properties", complete) is None


def test_fill_rules_document_guidance_and_examples_merge_into_one_generation_page() -> None:
    fill = GuidedAnswers(
        goal=GOAL_FILL, fill_rules="Summarize in bullets", sources=(SOURCE_PDF,), target_property_type="text"
    )
    questions = guided_question_ids(fill)
    assert "fill_rules" not in questions
    assert "document_guidance" not in questions
    assert "additional_rules" not in questions
    assert "examples" not in questions
    assert questions[-2:] == ("generation", "review")
    # The merged page sits after scope/sources/languages, not next to the target property.
    assert questions.index("generation") > questions.index("scope")
    assert question_error("generation", fill) is None
    assert question_error("generation", GuidedAnswers(goal=GOAL_FILL, target_property_type="text")) is not None
