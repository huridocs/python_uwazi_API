"""Conditional, one-question-at-a-time navigation for the guided task wizard."""

from __future__ import annotations

from .guided_task import (
    EXISTING_CUSTOM,
    FALLBACK_VALUE,
    GOAL_CLASSIFY,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    OUTPUT_SPECIFIC,
    SCOPE_FILTER,
    SOURCE_METADATA,
    GuidedAnswers,
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
REVIEW_QUESTION = "review"


def guided_question_ids(answers: GuidedAnswers) -> tuple[str, ...]:
    """Return only the questions relevant to the current answers, in order."""
    questions = [TEMPLATE_QUESTION]
    if answers.goal == GOAL_CUSTOM:
        questions.append(CUSTOM_DESCRIPTION_QUESTION)
    else:
        questions.append(TARGET_PROPERTY_QUESTION)
        if answers.goal == GOAL_FILL:
            questions.extend((FILL_RULES_QUESTION, EXISTING_VALUE_MODE_QUESTION))
            if answers.existing_value_mode == EXISTING_CUSTOM:
                questions.append(EXISTING_VALUE_RULE_QUESTION)
        elif answers.goal == GOAL_CLASSIFY:
            questions.append(FALLBACK_MODE_QUESTION)
            if answers.fallback_mode == FALLBACK_VALUE:
                questions.append(FALLBACK_VALUE_QUESTION)

    questions.extend((SCOPE_QUESTION,))
    if answers.entity_scope == SCOPE_FILTER:
        questions.append(SCOPE_FILTER_QUESTION)
    questions.append(SOURCES_QUESTION)
    if SOURCE_METADATA in answers.sources:
        questions.append(METADATA_PROPERTIES_QUESTION)
    questions.extend((DOCUMENT_GUIDANCE_QUESTION, INPUT_LANGUAGE_MODE_QUESTION))
    if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC:
        questions.append(INPUT_LANGUAGES_QUESTION)
    questions.append(OUTPUT_LANGUAGE_MODE_QUESTION)

    if answers.output_language_mode == OUTPUT_SPECIFIC:
        questions.append(OUTPUT_LANGUAGES_QUESTION)
    questions.extend((ADDITIONAL_RULES_QUESTION, EXAMPLES_QUESTION, REVIEW_QUESTION))
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
    if question == FILL_RULES_QUESTION and not answers.fill_rules.strip():
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
    "guided_question_ids",
    "next_question_id",
    "previous_question_id",
    "question_error",
]
