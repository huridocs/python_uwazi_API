"""Isolated unit tests for the guided task wizard's pure seam.

Per ``AGENTS.md``: no mocks, no network, no env credentials. These exercise the
deterministic answer validation, the option vocabularies derived from a literal
instance schema, and the prompt assembly with literal inputs.
"""

from uwazi_admin_agent.domain.guided_task import (
    CREATE_SOURCE_DOCUMENTS,
    CREATE_SOURCE_EXISTING,
    CREATE_SOURCE_UNRELATED,
    GOAL_CREATE,
    GOAL_CUSTOM,
    GOAL_FILL,
    INPUT_LANGUAGE_SPECIFIC,
    INPUT_LANGUAGE_SPECIFIC_FALLBACK,
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
    fill_is_classification,
    language_choices,
    metadata_property_choices,
    property_choices,
    property_type,
    template_property_choices,
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
        goal=GOAL_FILL,
        sources=(SOURCE_PDF,),
        target_property="topics",
        target_property_type="multiselect",
    )
    return GuidedAnswers(**{**answers.__dict__, **overrides})


def _extract_answers(**overrides: object) -> GuidedAnswers:
    answers = GuidedAnswers(
        goal=GOAL_FILL,
        sources=(SOURCE_PDF,),
        target_property="judge",
        target_property_type="text",
        fill_rules="One name only.",
    )
    return GuidedAnswers(**{**answers.__dict__, **overrides})


def _create_unrelated(**overrides: object) -> GuidedAnswers:
    answers = GuidedAnswers(
        goal=GOAL_CREATE,
        create_source=CREATE_SOURCE_UNRELATED,
        templates=("Judgment",),
        create_count="10",
        create_instructions="Fictional rulings.",
        create_target_properties=("title", "judge"),
    )
    return GuidedAnswers(**{**answers.__dict__, **overrides})


def test_valid_classify_answers_have_no_issues() -> None:
    assert validate_answers(_classify_answers()) == []


def test_target_property_is_required_but_its_type_is_optional() -> None:
    issues = validate_answers(_classify_answers(target_property=""))
    assert any("suggested value" in issue for issue in issues)
    assert validate_answers(_extract_answers()) == []


def test_classification_branch_is_chosen_from_the_target_property_type() -> None:
    assert fill_is_classification(_classify_answers())
    assert not fill_is_classification(_extract_answers())
    # With no explicit type the schema decides; unknown names fall back to extraction.
    assert fill_is_classification(GuidedAnswers(goal=GOAL_FILL, target_property="related"), _schema())
    assert not fill_is_classification(GuidedAnswers(goal=GOAL_FILL, target_property="judge"), _schema())
    assert not fill_is_classification(GuidedAnswers(goal=GOAL_FILL, target_property="mystery"), _schema())


def test_fallback_value_is_required_when_that_mode_is_chosen() -> None:
    issues = validate_answers(_classify_answers(fallback_mode="value", fallback_value="  "))
    assert any("fallback value" in issue for issue in issues)
    assert validate_answers(_classify_answers(fallback_mode="value", fallback_value="Uncategorized")) == []


def test_classification_ignores_the_extraction_only_answers() -> None:
    assert validate_answers(_classify_answers(fill_rules="")) == []


def test_metadata_source_requires_listed_properties() -> None:
    issues = validate_answers(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_METADATA,), custom_description="Do something"))
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


def test_extraction_requires_rules() -> None:
    issues = validate_answers(_extract_answers(fill_rules=""))
    assert any("rules or formatting" in issue for issue in issues)


def test_custom_task_requires_a_description() -> None:
    issues = validate_answers(GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,)))
    assert any("analyze" in issue for issue in issues)


def test_specific_language_modes_require_selections() -> None:
    for mode in (INPUT_LANGUAGE_SPECIFIC, INPUT_LANGUAGE_SPECIFIC_FALLBACK):
        input_issues = validate_answers(_classify_answers(input_language_mode=mode))
        assert any("primary input language" in issue for issue in input_issues)
    output_issues = validate_answers(_classify_answers(output_language_mode=OUTPUT_SPECIFIC))
    assert any("output language" in issue for issue in output_issues)


def test_strict_and_fallback_primary_languages_are_distinct_prompts() -> None:
    strict = assemble_prompt(
        _extract_answers(input_language_mode=INPUT_LANGUAGE_SPECIFIC, input_languages=("es",)),
        {"es": "Spanish"},
    )
    assert "Always read ONLY from: Spanish (es)." in strict
    assert "Do not fall back: skip the entity when the document is missing in it." in strict
    fallback = assemble_prompt(
        _extract_answers(input_language_mode=INPUT_LANGUAGE_SPECIFIC_FALLBACK, input_languages=("es",)),
        {"es": "Spanish"},
    )
    assert "Prefer reading from: Spanish (es)." in fallback
    assert "Fall back to another language when the document is missing in it." in fallback
    assert "skip the entity" not in fallback


def test_create_unrelated_requires_template_count_instructions_and_properties() -> None:
    assert validate_answers(_create_unrelated()) == []
    issues = validate_answers(
        GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_UNRELATED, templates=(), create_count="  ")
    )
    assert any("template the new entities should use" in issue for issue in issues)
    assert any("how many entities" in issue for issue in issues)
    assert any("new entities should contain" in issue for issue in issues)
    assert any("property the new entity should populate" in issue for issue in issues)


def test_create_from_existing_properties_requires_source_and_target_choices() -> None:
    ok = GuidedAnswers(
        goal=GOAL_CREATE,
        create_source=CREATE_SOURCE_EXISTING,
        templates=("Order",),
        source_templates=("Judgment",),
        create_source_properties=("title",),
        create_target_properties=("title",),
    )
    assert validate_answers(ok) == []
    issues = validate_answers(GuidedAnswers(goal=GOAL_CREATE, create_source=CREATE_SOURCE_EXISTING, templates=("Order",)))
    assert any("source template" in issue for issue in issues)
    assert any("source property to carry over" in issue for issue in issues)


def test_create_from_documents_skips_the_source_property_requirement() -> None:
    ok = GuidedAnswers(
        goal=GOAL_CREATE,
        create_source=CREATE_SOURCE_DOCUMENTS,
        templates=("Order",),
        source_templates=("Judgment",),
        create_target_properties=("summary",),
        sources=(SOURCE_PDF,),
    )
    issues = validate_answers(ok)
    assert not any("source property to carry over" in issue for issue in issues)


def test_create_does_not_read_instance_sources() -> None:
    """The create flow validates its own answers, never the fill/custom source list."""
    create = GuidedAnswers(goal=GOAL_CREATE, source_templates=("Judgment",))
    issues = validate_answers(create)
    assert not any("input source" in issue for issue in issues)


def test_property_choices_are_fill_scoped_and_deduplicated() -> None:
    classify = property_choices(_schema(), ())
    names = [name for name, _ in classify]
    # Date added is Uwazi-managed: never a write target.
    assert names == ["judge", "related", "topics"]
    # Templates narrow the set: Order defines no multiselect property.
    narrowed = [name for name, _ in property_choices(_schema(), ["Order"])]
    assert narrowed == ["judge", "related"]


def test_the_managed_dates_stay_readable_input_but_are_not_write_targets() -> None:
    assert "creationDate" in [name for name, _ in template_property_choices(_schema(), "Judgment")]
    assert "creationDate" not in [name for name, _ in template_property_choices(_schema(), "Judgment", writable_only=True)]


def test_template_property_choices_and_property_type_resolve_one_template() -> None:
    assert [name for name, _ in template_property_choices(_schema(), "Order")] == ["judge", "related"]
    assert template_property_choices(_schema(), "Unknown") == []
    assert property_type(_schema(), "topics") == "multiselect"
    assert property_type(_schema(), "judge") == "text"
    assert property_type(_schema(), "invented") == ""


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
    assert "Fill multiselect property `topics`" in prompt
    assert "- **Target Templates:** Judgment" in prompt
    assert "currently empty" in prompt
    assert "`topics`" in prompt
    assert "Classification rules" in prompt
    assert "Assign all relevant existing thesaurus terms" in prompt
    # Contract rules belong in the system prompt, not the task prompt.
    assert "Suggested value = the COMPLETE set" not in prompt
    assert "### Output Requirement" not in prompt
    # Multiselect is a shared property: no multilingual output rules section.
    assert "### Multilingual Output Rules" not in prompt
    # Empty optional rules/examples are omitted rather than printed as None.
    assert "**Examples:** None" not in prompt
    assert "**Additional Rules:** None" not in prompt


def test_relationship_metadata_prompt_is_specific_and_omits_document_and_contract_noise() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_FILL,
            templates=("Rec",),
            sources=(SOURCE_METADATA,),
            metadata_properties=("rec",),
            input_language_mode=INPUT_LANGUAGE_SPECIFIC,
            input_languages=("en",),
            target_property="states",
            target_property_type="relationship",
            additional_rules="Extract states from trailing parentheses.",
        ),
        {"en": "English"},
    )
    assert "Fill relationship property `states` by linking matching existing entities" in prompt
    assert "entity metadata properties (`rec`)" in prompt
    assert "Document reading guidance" not in prompt
    assert "document" not in prompt.lower()
    assert "thesaurus" not in prompt.lower()
    assert "multiselect" not in prompt.lower()
    assert "Link matching existing entities from the target relationship template." in prompt
    assert "Suggested value = the COMPLETE set" not in prompt
    assert "- **Fallback:** Leave the suggestion blank when no value clearly applies." in prompt
    assert "### Multilingual Output Rules" not in prompt
    assert "### Output Requirement" not in prompt
    assert "### Custom Instructions" in prompt
    assert "Extract states from trailing parentheses." in prompt
    assert "**Examples:**" not in prompt


def test_classification_prompt_never_asks_for_extraction_rules() -> None:
    prompt = assemble_prompt(_classify_answers(), {})
    assert "Extraction/Formatting Rules" not in prompt
    assert "Existing Value Handling" not in prompt


def test_empty_scope_is_goal_aware() -> None:
    custom = assemble_prompt(
        GuidedAnswers(goal=GOAL_CUSTOM, sources=(SOURCE_PDF,), entity_scope=SCOPE_EMPTY, custom_description="Fill it"),
        {},
    )
    assert "property named by the task" in custom
    classify = assemble_prompt(_classify_answers(entity_scope=SCOPE_EMPTY), {})
    assert "target property is currently empty" in classify


def test_assembled_prompt_includes_document_guidance_only_when_reading_documents_and_supplied() -> None:
    blank_pdf = assemble_prompt(_classify_answers(), {})
    assert "Document reading guidance" not in blank_pdf
    explicit_pdf = assemble_prompt(_classify_answers(document_guidance="Read the last page"), {})
    assert "- **Document reading guidance:** Read the last page" in explicit_pdf
    metadata_only = assemble_prompt(
        _classify_answers(sources=(SOURCE_METADATA,), metadata_properties=("rec",), document_guidance="Read the last page"),
        {},
    )
    assert "Document reading guidance" not in metadata_only


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


def test_assembled_prompt_names_output_languages_and_source_mode_for_text_properties() -> None:
    specific = assemble_prompt(
        _extract_answers(output_language_mode=OUTPUT_SPECIFIC, output_languages=("en", "es")),
        {"en": "English", "es": "Spanish"},
    )
    assert "### Multilingual Output Rules" in specific
    assert "English (en), Spanish (es)" in specific
    assert "Inspect the target property definition" not in specific
    source_doc = assemble_prompt(_extract_answers(output_language_mode=OUTPUT_SOURCE), {})
    assert "language of the source document" in source_doc
    source_meta = assemble_prompt(
        _extract_answers(sources=(SOURCE_METADATA,), metadata_properties=("rec",), output_language_mode=OUTPUT_SOURCE),
        {},
    )
    assert "Write the suggestion into the entity's fields in the source language." in source_meta


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


def test_extraction_prompt_states_the_rules_and_never_a_current_value_policy() -> None:
    """The prompt carries the extraction rules only: no "existing value"
    instruction, because the suggestion must come from the input (and the
    operator decides per row what to keep)."""
    prompt = assemble_prompt(_extract_answers(), {})
    assert "One name only." in prompt
    assert "Existing Value Handling" not in prompt
    assert "Skip the entity" not in prompt


def test_unrelated_create_prompt_declares_count_and_instructions() -> None:
    prompt = assemble_prompt(_create_unrelated(), {"en": "English"})
    assert "Create entities" in prompt
    assert "- **Target Template (new entities):** `Judgment`" in prompt
    assert "- **Entities to create:** 10" in prompt
    assert "Fictional rulings." in prompt
    assert "`title`" in prompt and "`judge`" in prompt
    assert "### Creation Source" in prompt
    assert "No instance content" in prompt
    # Unrelated creates read no instance content, so no source section or scope.
    assert "### Input Data & Source Language Strategy" not in prompt
    assert "Entity Scope" not in prompt


def test_create_from_documents_prompt_reads_the_source_documents() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_CREATE,
            create_source=CREATE_SOURCE_DOCUMENTS,
            templates=("Order",),
            source_templates=("Judgment",),
            create_target_properties=("summary",),
        ),
        {},
    )
    assert "`Judgment`" in prompt
    assert "PDF/document" in prompt
    assert "Main PDF(s)" in prompt


def test_create_from_existing_metadata_prompt_names_carried_properties() -> None:
    prompt = assemble_prompt(
        GuidedAnswers(
            goal=GOAL_CREATE,
            create_source=CREATE_SOURCE_EXISTING,
            templates=("Order",),
            source_templates=("Judgment",),
            create_source_properties=("judge", "creationDate"),
            create_target_properties=("judge",),
        ),
        {},
    )
    assert "Copy or transform" in prompt
    assert "`judge`" in prompt and "`creationDate`" in prompt
    assert "Entity metadata properties" in prompt
