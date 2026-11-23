"""Isolated unit tests for the guided task wizard's pure seam.

Per ``AGENTS.md``: no mocks, no network, no env credentials. These exercise the
deterministic answer validation, the option vocabularies derived from a literal
instance schema, and the prompt assembly with literal inputs.
"""

from uwazi_admin_agent.domain.guided_task import (
    EXISTING_SKIP,
    GOAL_CLASSIFY,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    OUTPUT_SOURCE,
    OUTPUT_SPECIFIC,
    SCOPE_EMPTY,
    SCOPE_FILTER,
    SOURCE_METADATA,
    SOURCE_PDF,
    GuidedAnswers,
    WizardLanguage,
    WizardProperty,
    WizardSchema,
    WizardTemplate,
    assemble_prompt,
    language_choices,
    metadata_property_choices,
    property_choices,
    validate_answers,
)


def _schema() -> WizardSchema:
    return WizardSchema(
        templates=(
            WizardTemplate(
                name="Judgment",
                properties=(
                    WizardProperty(name="judge", label="Judge", type="text"),
                    WizardProperty(name="topics", label="Topics", type="multiselect"),
                    WizardProperty(name="creationDate", label="Date added", type="date"),
                ),
            ),
            WizardTemplate(
                name="Order",
                properties=(
                    WizardProperty(name="judge", label="Judge", type="text"),
                    WizardProperty(name="related", label="Related", type="relationship"),
                ),
            ),
        ),
        languages=(
            WizardLanguage(key="en", label="English", default=True),
            WizardLanguage(key="es", label="Spanish"),
        ),
    )


def _classify_answers(**overrides: object) -> GuidedAnswers:
    answers = GuidedAnswers(
        goal=GOAL_CLASSIFY,
        sources=(SOURCE_PDF,),
        target_property="topics",
    )
    return GuidedAnswers(**{**answers.__dict__, **overrides})


def test_valid_classify_answers_have_no_issues() -> None:
    assert validate_answers(_classify_answers()) == []


def test_classify_requires_a_target_property() -> None:
    issues = validate_answers(_classify_answers(target_property=""))
    assert any("suggested topics" in issue for issue in issues)


def test_fallback_value_is_required_when_that_mode_is_chosen() -> None:
    issues = validate_answers(_classify_answers(fallback_mode="value", fallback_value="  "))
    assert any("fallback value" in issue for issue in issues)
    assert validate_answers(_classify_answers(fallback_mode="value", fallback_value="Uncategorized")) == []


def test_metadata_source_requires_listed_properties() -> None:
    issues = validate_answers(
        GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_METADATA,), custom_description="Do something")
    )
    assert any("metadata properties" in issue for issue in issues)
    ok = GuidedAnswers(
        goal=GOAL_CUSTOM,
        sources=(SOURCE_METADATA,),
        metadata_properties=("title",),
        custom_description="Do something",
    )
    assert validate_answers(ok) == []


def test_filter_scope_requires_the_condition() -> None:
    issues = validate_answers(_classify_answers(entity_scope=SCOPE_FILTER))
    assert any("filter or condition" in issue for issue in issues)
    assert validate_answers(_classify_answers(entity_scope=SCOPE_FILTER, scope_filter="creationDate > 2025-01-01")) == []


def test_fill_requires_rules() -> None:
    issues = validate_answers(
        GuidedAnswers(
            goal=GOAL_FILL,
            sources=(SOURCE_PDF,),
            target_property="judge",
            fill_rules="",
            existing_value_mode=EXISTING_SKIP,
        )
    )
    assert any("rules or formatting" in issue for issue in issues)


def test_custom_task_requires_a_description() -> None:
    issues = validate_answers(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,)))
    assert any("analyze" in issue for issue in issues)


def test_specific_language_modes_require_selections() -> None:
    input_issues = validate_answers(_classify_answers(input_language_mode=INPUT_LANGUAGE_SPECIFIC))
    assert any("primary input language" in issue for issue in input_issues)
    output_issues = validate_answers(_classify_answers(output_language_mode=OUTPUT_SPECIFIC))
    assert any("output language" in issue for issue in output_issues)


def test_property_choices_are_goal_scoped_and_deduplicated() -> None:
    classify = property_choices(_schema(), (), GOAL_CLASSIFY)
    assert [name for name, _ in classify] == ["related", "topics"]
    fill = [name for name, _ in property_choices(_schema(), (), GOAL_FILL)]
    assert "judge" in fill and fill.count("judge") == 1
    # Templates narrow the set: Order defines no multiselect property.
    narrowed = [name for name, _ in property_choices(_schema(), ["Order"], GOAL_CLASSIFY)]
    assert narrowed == ["related"]


def test_metadata_property_choices_cover_every_type() -> None:
    names = [name for name, _ in metadata_property_choices(_schema(), ["Judgment"])]
    assert names == ["creationDate", "judge", "topics"]


def test_language_choices_render_label_and_key() -> None:
    assert language_choices(_schema()) == {"en": "English (en)", "es": "Spanish (es)"}


def test_assembled_prompt_carries_the_answers_and_structure() -> None:
    prompt = assemble_prompt(
        _classify_answers(scope_filter="", entity_scope=SCOPE_EMPTY, templates=("Judgment",)),
        {"en": "English"},
    )
    assert "### Task Goal" in prompt
    assert "- **Target Templates:** Judgment" in prompt
    assert "currently empty" in prompt
    assert "`topics`" in prompt
    assert "### Multilingual Output Rules" in prompt
    assert "### Output Requirement" in prompt
    assert "do not mutate Uwazi entities directly" in prompt


def test_empty_scope_is_goal_aware() -> None:
    custom = assemble_prompt(
        GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,), entity_scope=SCOPE_EMPTY, custom_description="Fill it"),
        {},
    )
    assert "property named by the task" in custom
    classify = assemble_prompt(_classify_answers(entity_scope=SCOPE_EMPTY), {})
    assert "target property is currently empty" in classify


def test_assembled_prompt_uses_document_guidance_default() -> None:
    prompt = assemble_prompt(_classify_answers(), {})
    assert "Determine the optimal extraction strategy" in prompt
    explicit = assemble_prompt(_classify_answers(document_guidance="Read the last page"), {})
    assert "Read the last page" in explicit


def test_assembled_prompt_lists_metadata_properties_and_scope_filter() -> None:
    prompt = assemble_prompt(
        _classify_answers(
            sources=(SOURCE_METADATA,),
            metadata_properties=("title", "summary"),
            entity_scope=SCOPE_FILTER,
            scope_filter="creationDate > 2025-01-01",
        ),
        {},
    )
    assert "`title`" in prompt and "`summary`" in prompt
    assert "creationDate > 2025-01-01" in prompt


def test_assembled_prompt_names_output_languages_and_source_mode() -> None:
    specific = assemble_prompt(
        _classify_answers(output_language_mode=OUTPUT_SPECIFIC, output_languages=("en", "es")),
        {"en": "English", "es": "Spanish"},
    )
    assert "English (en), Spanish (es)" in specific
    source = assemble_prompt(_classify_answers(output_language_mode=OUTPUT_SOURCE), {})
    assert "language of the source document" in source


def test_custom_task_prompt_embeds_the_description() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_CUSTOM,
            sources=(SOURCE_PDF,),
            custom_description="Find entities whose title has a trailing year",
        ),
        {},
    )
    assert "Find entities whose title has a trailing year" in prompt
    assert "**Target Property:**" not in prompt


def test_fill_prompt_states_existing_value_handling() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_FILL,
            sources=(SOURCE_PDF,),
            target_property="judge",
            fill_rules="One name only.",
            existing_value_mode=EXISTING_SKIP,
        ),
        {},
    )
    assert "Skip the entity" in prompt
    assert "One name only." in prompt
