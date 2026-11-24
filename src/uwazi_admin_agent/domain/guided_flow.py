"""Conditional, one-question-at-a-time navigation for the guided task wizard."""

from __future__ import annotations

from .guided_task import (
    CREATE_SOURCE_DOCUMENTS,
    CREATE_SOURCE_EXISTING,
    CREATE_SOURCE_UNRELATED,
    EXISTING_CUSTOM,
    FALLBACK_VALUE,
    GOAL_CREATE,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    OUTPUT_SPECIFIC,
    SCOPE_FILTER,
    SOURCE_METADATA,
    GuidedAnswers,
    fill_is_classification,
)

TEMPLATE_QUESTION = "templates"
CUSTOM_DESCRIPTION_QUESTION = "custom_description"
TARGET_PROPERTY_QUESTION = "target_property"
FILL_RULES_QUESTION = "fill_rules"
EXISTING_VALUE_MODE_QUESTION = "existing_value_mode"
EXISTING_VALUE_RULE_QUESTION = "existing_value_rule"
FALLBACK_MODE_QUESTION = "fallback_mode"
FALLBACK_VALUE_QUESTION = "fallback_value"
SCOPE_QUESTION = "scope"
SCOPE_FILTER_QUESTION = "scope_filter"
SOURCES_QUESTION = "sources"
METADATA_PROPERTIES_QUESTION = "metadata_properties"
DOCUMENT_GUIDANCE_QUESTION = "document_guidance"
INPUT_LANGUAGE_MODE_QUESTION = "input_language_mode"
INPUT_LANGUAGES_QUESTION = "input_languages"
OUTPUT_LANGUAGE_MODE_QUESTION = "output_language_mode"
OUTPUT_LANGUAGES_QUESTION = "output_languages"
ADDITIONAL_RULES_QUESTION = "additional_rules"
EXAMPLES_QUESTION = "examples"
GENERATION_QUESTION = "generation"
REVIEW_QUESTION = "review"

# Create-entities questions.
CREATE_SOURCE_QUESTION = "create_source"
SOURCE_TEMPLATES_QUESTION = "source_templates"
CREATE_SOURCE_PROPERTIES_QUESTION = "create_source_properties"
CREATE_TARGET_PROPERTIES_QUESTION = "create_target_properties"
CREATE_COUNT_QUESTION = "create_count"
CREATE_INSTRUCTIONS_QUESTION = "create_instructions"


def _fill_questions(answers: GuidedAnswers) -> list[str]:
    questions = [TEMPLATE_QUESTION, TARGET_PROPERTY_QUESTION]
    if fill_is_classification(answers):
        # select/multiselect/relationship targets are a classification task:
        # no extraction rules, no existing-value policy.
        questions.append(FALLBACK_MODE_QUESTION)
        if answers.fallback_mode == FALLBACK_VALUE:
            questions.append(FALLBACK_VALUE_QUESTION)
    else:
        questions.append(EXISTING_VALUE_MODE_QUESTION)
        if answers.existing_value_mode == EXISTING_CUSTOM:
            questions.append(EXISTING_VALUE_RULE_QUESTION)
    return questions


def _create_questions(answers: GuidedAnswers) -> list[str]:
    questions = [TEMPLATE_QUESTION, CREATE_SOURCE_QUESTION]
    if answers.create_source in (CREATE_SOURCE_EXISTING, CREATE_SOURCE_DOCUMENTS):
        questions.append(SOURCE_TEMPLATES_QUESTION)
        if answers.create_source == CREATE_SOURCE_EXISTING:
            questions.append(CREATE_SOURCE_PROPERTIES_QUESTION)
        questions.append(CREATE_TARGET_PROPERTIES_QUESTION)
        questions.append(SCOPE_QUESTION)
        if answers.entity_scope == SCOPE_FILTER:
            questions.append(SCOPE_FILTER_QUESTION)
        questions.append(INPUT_LANGUAGE_MODE_QUESTION)
        if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC:
            questions.append(INPUT_LANGUAGES_QUESTION)
    elif answers.create_source == CREATE_SOURCE_UNRELATED:
        questions.extend((CREATE_COUNT_QUESTION, CREATE_INSTRUCTIONS_QUESTION, CREATE_TARGET_PROPERTIES_QUESTION))
    else:
        questions.append(CREATE_TARGET_PROPERTIES_QUESTION)
    return questions


def guided_question_ids(answers: GuidedAnswers) -> tuple[str, ...]:
    """Return only the questions relevant to the current answers, in order."""
    if answers.goal == GOAL_CREATE:
        questions = _create_questions(answers)
    elif answers.goal == GOAL_CUSTOM:
        questions = [TEMPLATE_QUESTION, CUSTOM_DESCRIPTION_QUESTION, SCOPE_QUESTION]
    else:
        questions = _fill_questions(answers)

    if answers.goal == GOAL_CUSTOM:
        if answers.entity_scope == SCOPE_FILTER:
            questions.append(SCOPE_FILTER_QUESTION)
        questions.append(SOURCES_QUESTION)
        if SOURCE_METADATA in answers.sources:
            questions.append(METADATA_PROPERTIES_QUESTION)
        questions.append(INPUT_LANGUAGE_MODE_QUESTION)
        if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC:
            questions.append(INPUT_LANGUAGES_QUESTION)
    elif answers.goal == GOAL_FILL:
        questions.append(SCOPE_QUESTION)
        if answers.entity_scope == SCOPE_FILTER:
            questions.append(SCOPE_FILTER_QUESTION)
        questions.append(SOURCES_QUESTION)
        if SOURCE_METADATA in answers.sources:
            questions.append(METADATA_PROPERTIES_QUESTION)
        questions.append(INPUT_LANGUAGE_MODE_QUESTION)
        if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC:
            questions.append(INPUT_LANGUAGES_QUESTION)

    questions.append(OUTPUT_LANGUAGE_MODE_QUESTION)
    if answers.output_language_mode == OUTPUT_SPECIFIC:
        questions.append(OUTPUT_LANGUAGES_QUESTION)
    questions.extend((GENERATION_QUESTION, REVIEW_QUESTION))
    return tuple(questions)


def next_question_id(current: str, answers: GuidedAnswers) -> str:
    """Advance from ``current``, skipping follow-ups made irrelevant by an answer."""
    questions = guided_question_ids(answers)
    if current not in questions:
        return questions[0]
    return questions[min(questions.index(current) + 1, len(questions) - 1)]


def previous_question_id(current: str, answers: GuidedAnswers) -> str:
    """Go to the preceding relevant question, or stay on the first one."""
    questions = guided_question_ids(answers)
    if current not in questions:
        return questions[0]
    return questions[max(questions.index(current) - 1, 0)]


def question_error(question: str, answers: GuidedAnswers) -> str | None:
    """Return the answer needed before leaving ``question``, if any."""
    if question == CUSTOM_DESCRIPTION_QUESTION and not answers.custom_description.strip():
        return "Describe what the task should analyze, extract or suggest."
    if question == TARGET_PROPERTY_QUESTION and not answers.target_property.strip():
        return "Choose or enter a target property."
    if question == GENERATION_QUESTION and answers.goal == GOAL_FILL and not fill_is_classification(answers):
        if not answers.fill_rules.strip():
            return "Describe the rules or formatting for the generated value."
    if question == EXISTING_VALUE_RULE_QUESTION and not answers.existing_value_rule.strip():
        return "Describe the custom rule for properties that already have a value."
    if question == FALLBACK_VALUE_QUESTION and not answers.fallback_value.strip():
        return "Enter the fallback value to suggest."
    if question == SCOPE_FILTER_QUESTION and not answers.scope_filter.strip():
        return "Describe the filter or condition."
    if question == SOURCES_QUESTION and not answers.sources:
        return "Choose at least one input source."
    if question == METADATA_PROPERTIES_QUESTION and not answers.metadata_properties:
        return "Choose or enter at least one metadata property."
    if question == INPUT_LANGUAGES_QUESTION and not answers.input_languages:
        return "Choose or enter at least one input language."
    if question == OUTPUT_LANGUAGES_QUESTION and not answers.output_languages:
        return "Choose or enter at least one output language."
    return None


__all__ = [
    "CREATE_COUNT_QUESTION",
    "CREATE_INSTRUCTIONS_QUESTION",
    "CREATE_SOURCE_PROPERTIES_QUESTION",
    "CREATE_SOURCE_QUESTION",
    "CREATE_TARGET_PROPERTIES_QUESTION",
    "SOURCE_TEMPLATES_QUESTION",
    "guided_question_ids",
    "next_question_id",
    "previous_question_id",
    "question_error",
]
