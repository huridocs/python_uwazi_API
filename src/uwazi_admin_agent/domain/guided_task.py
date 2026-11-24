"""The guided task-creation wizard: answers, validation and prompt assembly.

The New Task wizard can either accept a free-form prompt or ask a fixed set of
questions and ASSEMBLE the prompt from the answers. The questions need no LLM:
the only instance data they use is the template list (with each template's
properties and their types) and the configured languages, both read from
Uwazi's API before the form is shown.

This module is pure (no I/O, no NiceGUI): the option vocabularies the UI renders
and the prompt assembly/validation share the same constants, so a label changed
here cannot drift from the prompt it produces. The assembled prompt follows the
wizard's documented structure: task goal, input sources and source-language
strategy, target property and logic, multilingual output rules, custom
instructions/examples, and the fixed "record suggestions, never write" output
requirement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# --- Question vocabularies -------------------------------------------------

GOAL_FILL = "fill"
GOAL_CREATE = "create"
GOAL_CUSTOM = "custom"
GOALS: tuple[str, ...] = (GOAL_FILL, GOAL_CREATE, GOAL_CUSTOM)

GOAL_LABELS: dict[str, str] = {
    GOAL_FILL: "Fill a property",
    GOAL_CREATE: "Create entities",
    GOAL_CUSTOM: "Custom task",
}

# The full descriptions are BOTH the radio option text and the `Flow` line of the
# assembled prompt: the operator must recognize in the prompt exactly what they
# picked. A select/multiselect/relationship target IS a classification task, so
# it needs no separate goal: the target property's type selects the sub-flow.
GOAL_DESCRIPTIONS: dict[str, str] = {
    GOAL_FILL: (
        "Fill a property — extract a value from documents or existing fields; select, multiselect and "
        "relationship targets are classified with existing thesaurus terms or linked entities."
    ),
    GOAL_CREATE: (
        "Create entities — build new entities under a template from existing entities' properties, their "
        "documents, or instructions unrelated to the current instance content."
    ),
    GOAL_CUSTOM: "Custom task — describe a custom analysis or data-processing rule in your own words.",
}

CREATE_SOURCE_EXISTING = "existing_metadata"
CREATE_SOURCE_DOCUMENTS = "existing_documents"
CREATE_SOURCE_UNRELATED = "unrelated"
CREATE_SOURCES: tuple[str, ...] = (CREATE_SOURCE_EXISTING, CREATE_SOURCE_DOCUMENTS, CREATE_SOURCE_UNRELATED)

CREATE_SOURCE_LABELS: dict[str, str] = {
    CREATE_SOURCE_EXISTING: "Existing entities' properties (one new entity per source entity)",
    CREATE_SOURCE_DOCUMENTS: "Existing entities' PDFs/documents (extract their content into new entities)",
    CREATE_SOURCE_UNRELATED: "Instructions unrelated to the current instance content (generate new entities)",
}

# What each create source reads, as stated in the assembled prompt.
CREATE_SOURCE_DESCRIPTIONS: dict[str, str] = {
    CREATE_SOURCE_EXISTING: "The properties of existing entities in the source template.",
    CREATE_SOURCE_DOCUMENTS: "The PDFs/documents attached to existing entities in the source template.",
    CREATE_SOURCE_UNRELATED: "No instance content: only the instructions provided by the operator.",
}

SCOPE_ALL = "all"
SCOPE_EMPTY = "empty"
SCOPE_FILTER = "filter"
SCOPES: tuple[str, ...] = (SCOPE_ALL, SCOPE_EMPTY, SCOPE_FILTER)

SCOPE_LABELS: dict[str, str] = {
    SCOPE_ALL: "All entities in the selected template(s)",
    SCOPE_EMPTY: "Only entities where the target property is currently empty",
    SCOPE_FILTER: "Entities matching a specific filter or condition",
}

SOURCE_PDF = "main_pdf"
SOURCE_ATTACHMENTS = "attachments"
SOURCE_METADATA = "metadata"
SOURCES: tuple[str, ...] = (SOURCE_PDF, SOURCE_ATTACHMENTS, SOURCE_METADATA)

SOURCE_LABELS: dict[str, str] = {
    SOURCE_PDF: "Main PDF(s) (primary attached document)",
    SOURCE_ATTACHMENTS: "Supporting files / attachments",
    SOURCE_METADATA: "Entity metadata properties",
}

INPUT_LANGUAGE_MATCH = "match"
INPUT_LANGUAGE_SPECIFIC = "specific"
INPUT_LANGUAGE_ANY = "any"
INPUT_LANGUAGE_MODES: tuple[str, ...] = (INPUT_LANGUAGE_MATCH, INPUT_LANGUAGE_SPECIFIC, INPUT_LANGUAGE_ANY)

INPUT_LANGUAGE_LABELS: dict[str, str] = {
    INPUT_LANGUAGE_MATCH: (
        "Match each target language (read English sources for English suggestions, Spanish for Spanish, etc.)"
    ),
    INPUT_LANGUAGE_SPECIFIC: "Use a specific primary language",
    INPUT_LANGUAGE_ANY: "Any available language (fall back automatically when missing)",
}

FALLBACK_BLANK = "blank"
FALLBACK_VALUE = "value"
FALLBACK_MODES: tuple[str, ...] = (FALLBACK_BLANK, FALLBACK_VALUE)

FALLBACK_LABELS: dict[str, str] = {
    FALLBACK_BLANK: "Leave the suggestion blank (no change suggested for that entity)",
    FALLBACK_VALUE: "Suggest a specific fallback value",
}

EXISTING_SKIP = "skip"
EXISTING_REPLACE = "replace"
EXISTING_CUSTOM = "custom"
EXISTING_MODES: tuple[str, ...] = (EXISTING_SKIP, EXISTING_REPLACE, EXISTING_CUSTOM)

EXISTING_VALUE_LABELS: dict[str, str] = {
    EXISTING_SKIP: "Skip the entity (do not generate a suggestion if data already exists)",
    EXISTING_REPLACE: "Suggest a replacement (propose a new value to overwrite the existing one)",
    EXISTING_CUSTOM: "Custom rule",
}

OUTPUT_ALL = "all"
OUTPUT_SPECIFIC = "specific"
OUTPUT_SOURCE = "source"
OUTPUT_LANGUAGE_MODES: tuple[str, ...] = (OUTPUT_ALL, OUTPUT_SPECIFIC, OUTPUT_SOURCE)

OUTPUT_LANGUAGE_LABELS: dict[str, str] = {
    OUTPUT_ALL: "Generate for all configured Uwazi languages (translate or extract into each language)",
    OUTPUT_SPECIFIC: "Generate only for specific language(s)",
    OUTPUT_SOURCE: "Generate only in the language of the source document (do not translate)",
}

DEFAULT_DOCUMENT_GUIDANCE = "Determine the optimal extraction strategy based on document length and structure."

# Property types the fill flow can target. A select/multiselect/relationship
# target is a CLASSIFICATION task (it can only take thesaurus terms or linked
# entities); every other type is an extraction task (the LLM produces a plain
# value). The target's type therefore selects the sub-flow: there is no separate
# "classify" goal.
CLASSIFY_TARGET_PROPERTY_TYPES: frozenset[str] = frozenset({"select", "multiselect", "relationship"})
FILL_TARGET_PROPERTY_TYPES: frozenset[str] = frozenset(
    {
        "text",
        "markdown",
        "numeric",
        "date",
        "daterange",
        "multidate",
        "multidaterange",
        "select",
        "multiselect",
        "link",
        "geolocation",
        "relationship",
    }
)


# --- Instance metadata read by the wizard (templates + languages) ----------


@dataclass(frozen=True)
class WizardProperty:
    """One property of a template, as the wizard needs it (name, label, type)."""

    name: str
    label: str = ""
    type: str = ""

    @property
    def display(self) -> str:
        """The option text: ``Label (name) — type`` (name alone when it IS the label)."""
        label = (self.label or self.name).strip()
        name = self.name.strip()
        rendered = f"{label} ({name})" if label != name else name
        return f"{rendered} — {self.type}" if self.type else rendered


@dataclass(frozen=True)
class WizardTemplate:
    """One Uwazi template and its properties."""

    name: str
    properties: tuple[WizardProperty, ...] = ()


@dataclass(frozen=True)
class WizardLanguage:
    """One configured Uwazi language."""

    key: str
    label: str = ""
    default: bool = False


@dataclass(frozen=True)
class WizardSchema:
    """The instance data the guided questions are built from."""

    templates: tuple[WizardTemplate, ...] = ()
    languages: tuple[WizardLanguage, ...] = ()


def language_choices(schema: WizardSchema | None) -> dict[str, str]:
    """``{key: "Label (key)"}`` for the configured languages (pure)."""
    if schema is None:
        return {}
    return {
        language.key: f"{language.label} ({language.key})" if language.label else language.key
        for language in schema.languages
        if language.key
    }


def language_label_map(schema: WizardSchema | None) -> dict[str, str]:
    """``{key: label}`` for the configured languages (pure; ``{}`` when unknown)."""
    if schema is None:
        return {}
    return {language.key: language.label for language in schema.languages if language.key}


def _selected_templates(schema: WizardSchema, templates: Iterable[str]) -> list[WizardTemplate]:
    wanted = {name for name in templates if name}
    if not wanted:
        return list(schema.templates)
    return [template for template in schema.templates if template.name in wanted]


def property_choices(
    schema: WizardSchema | None,
    templates: Iterable[str],
) -> list[tuple[str, str]]:
    """``[(name, option_text)]`` of properties the fill flow can target (pure).

    Properties are collected across the selected templates (all templates when
    the selection is empty). A name defined by several templates appears ONCE:
    the prompt names the property, and the generation LLM reads each target
    template's own definition. Only fill-writable types are offered; the type
    picked here decides whether the flow classifies (select/multiselect/
    relationship) or extracts (every other type).
    """
    labels: dict[str, str] = {}
    if schema is None:
        return []
    for template in _selected_templates(schema, templates):
        for prop in template.properties:
            if prop.type not in FILL_TARGET_PROPERTY_TYPES:
                continue
            labels.setdefault(prop.name, prop.display)
    return sorted(labels.items(), key=lambda item: item[1].casefold())


def template_property_choices(
    schema: WizardSchema | None,
    template: str,
) -> list[tuple[str, str]]:
    """``[(name, option_text)]`` of every property of ONE template (pure).

    Used by the create flow to offer the properties a new entity can read from
    a source entity and the properties it can populate on the target template.
    """
    labels: dict[str, str] = {}
    if schema is None:
        return []
    for candidate in schema.templates:
        if candidate.name != template.strip():
            continue
        for prop in candidate.properties:
            labels.setdefault(prop.name, prop.display)
    return sorted(labels.items(), key=lambda item: item[1].casefold())


def property_type(schema: WizardSchema | None, property_name: str) -> str:
    """The Uwazi type of ``property_name``, or ``""`` when unknown (pure).

    The first selected template that defines the property wins; an unknown or
    operator-typed name reports ``""`` and the fill flow falls back to the
    extraction branch.
    """
    name = property_name.strip()
    if schema is None or not name:
        return ""
    for template in schema.templates:
        for prop in template.properties:
            if prop.name == name and prop.type:
                return prop.type
    return ""


def metadata_property_choices(
    schema: WizardSchema | None,
    templates: Iterable[str],
) -> list[tuple[str, str]]:
    """``[(name, option_text)]`` of every property the input may read (pure)."""
    labels: dict[str, str] = {}
    if schema is None:
        return []
    for template in _selected_templates(schema, templates):
        for prop in template.properties:
            labels.setdefault(prop.name, prop.display)
    return sorted(labels.items(), key=lambda item: item[1].casefold())


def fill_is_classification(answers: GuidedAnswers, schema: WizardSchema | None = None) -> bool:
    """Whether the fill flow is classifying (pure).

    An explicit ``target_property_type`` wins; otherwise the instance schema is
    consulted. Unknown types (operator-typed name, unreadable schema) fall back
    to extraction: a text property accepts free values, so that is the safe
    branch.
    """
    property_type_name = answers.target_property_type.strip()
    resolved = property_type_name or property_type(schema, answers.target_property)
    return resolved in CLASSIFY_TARGET_PROPERTY_TYPES


# --- Answers --------------------------------------------------------------


@dataclass(frozen=True)
class GuidedAnswers:
    """Every answer the guided wizard collects (defaults = untouched controls)."""

    goal: str = GOAL_FILL
    templates: tuple[str, ...] = ()
    source_templates: tuple[str, ...] = ()
    entity_scope: str = SCOPE_ALL
    scope_filter: str = ""
    sources: tuple[str, ...] = ()
    metadata_properties: tuple[str, ...] = ()
    document_guidance: str = ""
    input_language_mode: str = INPUT_LANGUAGE_MATCH
    input_languages: tuple[str, ...] = ()
    target_property: str = ""
    target_property_type: str = ""
    fallback_mode: str = FALLBACK_BLANK
    fallback_value: str = ""
    fill_rules: str = ""
    existing_value_mode: str = EXISTING_REPLACE
    existing_value_rule: str = ""
    custom_description: str = ""
    create_source: str = CREATE_SOURCE_EXISTING
    create_source_properties: tuple[str, ...] = ()
    create_target_properties: tuple[str, ...] = ()
    create_count: str = ""
    create_instructions: str = ""
    output_language_mode: str = OUTPUT_ALL
    output_languages: tuple[str, ...] = ()
    additional_rules: str = ""
    examples: str = ""

    def target_property_for_goal(self) -> str:
        """The target property only when the active branch defines one (pure)."""
        return self.target_property.strip() if self.goal == GOAL_FILL else ""


# --- Validation -----------------------------------------------------------


def validate_answers(answers: GuidedAnswers) -> list[str]:
    """Return the human-readable problems blocking prompt assembly (pure).

    An empty list means the answers are complete. Optional fields (document
    guidance, additional rules, examples) are never required, and the template
    selection may be empty, which means "all templates".
    """
    errors: list[str] = []
    if answers.goal not in GOALS:
        errors.append("Choose what the task should do.")

    if answers.goal == GOAL_CREATE:
        errors.extend(_validate_create(answers))
    else:
        if answers.entity_scope not in SCOPES:
            errors.append("Choose which entities to include.")
        if answers.entity_scope == SCOPE_FILTER and not answers.scope_filter.strip():
            errors.append("Describe the filter or condition that selects the entities.")
        if not answers.sources:
            errors.append("Choose at least one input source to read.")
        if SOURCE_METADATA in answers.sources and not answers.metadata_properties:
            errors.append("List the metadata properties to read, or uncheck 'Entity metadata properties' as a source.")
        if answers.input_language_mode not in INPUT_LANGUAGE_MODES:
            errors.append("Choose how the input language is selected.")
        if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC and not answers.input_languages:
            errors.append("Choose the primary input language(s), or switch to 'Match each target language'.")

        if answers.goal == GOAL_FILL:
            if not answers.target_property.strip():
                errors.append("Choose the property that should receive the suggested value.")
            if fill_is_classification(answers):
                if answers.fallback_mode not in FALLBACK_MODES:
                    errors.append("Choose what to suggest when no topic clearly applies.")
                if answers.fallback_mode == FALLBACK_VALUE and not answers.fallback_value.strip():
                    errors.append("Enter the fallback value to suggest.")
            else:
                if not answers.fill_rules.strip():
                    errors.append("Describe the rules or formatting the generated value should follow.")
                if answers.existing_value_mode not in EXISTING_MODES:
                    errors.append("Choose how to handle a property that already has a value.")
                if answers.existing_value_mode == EXISTING_CUSTOM and not answers.existing_value_rule.strip():
                    errors.append("Describe the custom rule for properties that already have a value.")
        elif answers.goal == GOAL_CUSTOM:
            if not answers.custom_description.strip():
                errors.append("Describe what the task should analyze, extract or suggest.")

    if answers.output_language_mode not in OUTPUT_LANGUAGE_MODES:
        errors.append("Choose how multilingual outputs should be generated.")
    if answers.output_language_mode == OUTPUT_SPECIFIC and not answers.output_languages:
        errors.append("Choose the output language(s), or switch to 'Generate for all configured languages'.")
    return errors


def _validate_create(answers: GuidedAnswers) -> list[str]:
    """Problems specific to the create-entities flow (pure)."""
    errors: list[str] = []
    if not any(name.strip() for name in answers.templates):
        errors.append("Choose the template the new entities should use.")
    if answers.create_source not in CREATE_SOURCES:
        errors.append("Choose what the new entities should be based on.")
    if answers.create_source == CREATE_SOURCE_EXISTING:
        if not any(name.strip() for name in answers.source_templates):
            errors.append("Choose the source template(s) whose entities the new ones should be based on.")
        if not answers.create_source_properties:
            errors.append("Choose at least one source property to carry over.")
        if not answers.create_target_properties:
            errors.append("Choose at least one property the new entity should populate.")
        if answers.entity_scope not in SCOPES:
            errors.append("Choose which source entities to include.")
        if answers.entity_scope == SCOPE_FILTER and not answers.scope_filter.strip():
            errors.append("Describe the filter or condition that selects the source entities.")
        errors.extend(_validate_input_languages(answers))
    elif answers.create_source == CREATE_SOURCE_DOCUMENTS:
        if not any(name.strip() for name in answers.source_templates):
            errors.append("Choose the source template(s) whose documents should be read.")
        if not answers.create_target_properties:
            errors.append("Choose at least one property the new entity should populate.")
        if answers.entity_scope not in SCOPES:
            errors.append("Choose which source entities to include.")
        if answers.entity_scope == SCOPE_FILTER and not answers.scope_filter.strip():
            errors.append("Describe the filter or condition that selects the source entities.")
        errors.extend(_validate_input_languages(answers))
    elif answers.create_source == CREATE_SOURCE_UNRELATED:
        count = answers.create_count.strip()
        if not count.isdigit() or int(count) <= 0:
            errors.append("Enter how many entities the task should create.")
        if not answers.create_instructions.strip():
            errors.append("Describe what the new entities should contain.")
        if not answers.create_target_properties:
            errors.append("Choose at least one property the new entity should populate.")
    return errors


def _validate_input_languages(answers: GuidedAnswers) -> list[str]:
    errors: list[str] = []
    if answers.input_language_mode not in INPUT_LANGUAGE_MODES:
        errors.append("Choose how the input language is selected.")
    if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC and not answers.input_languages:
        errors.append("Choose the primary input language(s), or switch to 'Match each target language'.")
    return errors


# --- Prompt assembly ------------------------------------------------------


def _code(value: str) -> str:
    """Wrap a user-supplied identifier in backticks, never breaking the span."""
    return f"`{value.replace('`', '').strip()}`"


def _language_text(keys: Iterable[str], labels: Mapping[str, str]) -> str:
    """Render language keys as ``Label (key)``, comma-separated (pure)."""
    rendered: list[str] = []
    for key in keys:
        key = key.strip()
        if not key:
            continue
        label = (labels.get(key) or "").strip()
        rendered.append(f"{label} ({key})" if label else key)
    return ", ".join(rendered)


def _input_language_text(answers: GuidedAnswers, labels: Mapping[str, str]) -> str:
    if answers.input_language_mode == INPUT_LANGUAGE_SPECIFIC:
        return (
            f"Always read from: {_language_text(answers.input_languages, labels)}. "
            "Fall back to another language when the document or text is missing in it."
        )
    if answers.input_language_mode == INPUT_LANGUAGE_ANY:
        return (
            "Use any available language: whichever language has a document or text populated, "
            "falling back automatically when it is missing."
        )
    return (
        "Match each target language: read the English document/fields to generate English "
        "suggestions, the Spanish ones for Spanish, and so on."
    )


def _output_language_text(answers: GuidedAnswers, labels: Mapping[str, str]) -> str:
    if answers.output_language_mode == OUTPUT_SPECIFIC:
        return f"Generate only for these languages: {_language_text(answers.output_languages, labels)}."
    if answers.output_language_mode == OUTPUT_SOURCE:
        return "Generate only in the language of the source document (do not translate into other languages)."
    configured = _language_text(labels.keys(), labels)
    all_languages = "Generate for all configured Uwazi languages (translate or extract into each language)."
    return f"{all_languages} Configured languages: {configured}." if configured else all_languages


def _scope_text(answers: GuidedAnswers) -> str:
    if answers.entity_scope == SCOPE_EMPTY:
        if answers.goal == GOAL_CUSTOM:
            return "Only entities where the property named by the task is currently empty."
        return "Only entities where the target property is currently empty."
    if answers.entity_scope == SCOPE_FILTER:
        return f"Entities matching this filter/condition: {answers.scope_filter.strip()}"
    if answers.goal == GOAL_CREATE:
        return "All entities in the selected source template(s)."
    return "All entities in the selected template(s)."


def _sources_text(answers: GuidedAnswers) -> str:
    parts: list[str] = []
    if SOURCE_PDF in answers.sources:
        parts.append(SOURCE_LABELS[SOURCE_PDF])
    if SOURCE_ATTACHMENTS in answers.sources:
        parts.append(SOURCE_LABELS[SOURCE_ATTACHMENTS])
    if SOURCE_METADATA in answers.sources:
        properties = ", ".join(_code(name) for name in answers.metadata_properties if name.strip())
        parts.append(f"{SOURCE_LABELS[SOURCE_METADATA]} ({properties})" if properties else SOURCE_LABELS[SOURCE_METADATA])
    return "; ".join(parts)


def _logic_lines(answers: GuidedAnswers) -> list[str]:
    if answers.goal == GOAL_CUSTOM:
        return [f"- **Task Description:** {answers.custom_description.strip()}"]
    target = _code(answers.target_property_for_goal())
    if fill_is_classification(answers):
        fallback = (
            f"Suggest {_code(answers.fallback_value)} when no topic clearly applies."
            if answers.fallback_mode == FALLBACK_VALUE
            else "Leave the suggestion blank when no topic clearly applies."
        )
        return [
            f"- **Target Property:** {target}",
            f"- **Target Property Type:** {answers.target_property_type.strip() or 'select/multiselect/relationship'}",
            "- **Classification rules:** Assign the most relevant existing thesaurus term for a "
            "select/multiselect property, or link the matching relationship entity for a "
            "relationship property. Never invent a term or an entity that does not exist.",
            f"- **Fallback:** {fallback}",
        ]
    if answers.existing_value_mode == EXISTING_SKIP:
        existing = "Skip the entity when the property already has a value (generate no suggestion)."
    elif answers.existing_value_mode == EXISTING_CUSTOM:
        existing = f"Apply this rule: {answers.existing_value_rule.strip()}"
    else:
        existing = "Suggest a replacement value that overwrites the existing one in the review table."
    return [
        f"- **Target Property:** {target}",
        f"- **Extraction/Formatting Rules:** {answers.fill_rules.strip()}",
        f"- **Existing Value Handling:** {existing}",
    ]


def _create_goal_lines(answers: GuidedAnswers) -> list[str]:
    """The create flow's Task Goal lines (pure)."""
    target = ", ".join(_code(name) for name in answers.templates if name.strip()) or "All templates"
    if answers.create_source == CREATE_SOURCE_UNRELATED:
        count = answers.create_count.strip() or "the requested number of"
        return [
            f"- **Flow:** {GOAL_DESCRIPTIONS[GOAL_CREATE]}",
            f"- **Target Template (new entities):** {target}",
            f"- **Entities to create:** {count}",
        ]
    return [
        f"- **Flow:** {GOAL_DESCRIPTIONS[GOAL_CREATE]}",
        f"- **Target Template (new entities):** {target}",
        "- **Entities to create:** One new entity per matching source entity.",
        f"- **Entity Scope (source entities):** {_scope_text(answers)}",
    ]


def _create_source_lines(answers: GuidedAnswers) -> list[str]:
    """The create flow's Creation Source lines (pure)."""
    lines = [
        f"- **Basis:** {CREATE_SOURCE_LABELS[answers.create_source]}",
        f"- **Reads:** {CREATE_SOURCE_DESCRIPTIONS[answers.create_source]}",
    ]
    if answers.create_source in (CREATE_SOURCE_EXISTING, CREATE_SOURCE_DOCUMENTS):
        source_templates = ", ".join(_code(name) for name in answers.source_templates if name.strip()) or "All templates"
        lines.append(f"- **Source Templates:** {source_templates}")
    if answers.create_source == CREATE_SOURCE_EXISTING:
        if answers.create_source_properties:
            lines.append(
                "- **Source properties to read:** "
                + ", ".join(_code(name) for name in answers.create_source_properties if name.strip())
            )
        lines.append(
            "- **Target properties to populate:** "
            + ", ".join(_code(name) for name in answers.create_target_properties if name.strip())
        )
        lines.append(
            "- **Creation rule:** Copy or transform the selected source properties into the new entity: a target "
            "property that shares a source property's name takes that source value; any other target property is "
            "derived from the selected source properties and the creation instructions."
        )
    elif answers.create_source == CREATE_SOURCE_DOCUMENTS:
        lines.append(
            "- **Target properties to populate:** "
            + ", ".join(_code(name) for name in answers.create_target_properties if name.strip())
        )
        lines.append(
            "- **Creation rule:** Read each source entity's PDF/document and populate the new entity from its content."
        )
    else:
        lines.append(
            "- **Target properties to populate:** "
            + ", ".join(_code(name) for name in answers.create_target_properties if name.strip())
        )
    if answers.create_instructions.strip():
        lines.append(f"- **Creation instructions:** {answers.create_instructions.strip()}")
    return lines


def _assemble_create_prompt(answers: GuidedAnswers, labels: Mapping[str, str]) -> str:
    """Render a create-entities prompt (pure)."""
    lines: list[str] = [
        "### Task Goal",
        *_create_goal_lines(answers),
        "",
        "### Creation Source",
        *_create_source_lines(answers),
        "",
    ]
    if answers.create_source == CREATE_SOURCE_DOCUMENTS:
        lines.extend(
            [
                "### Input Data & Source Language Strategy",
                f"- **Sources to inspect:** {SOURCE_LABELS[SOURCE_PDF]}",
                f"- **Document reading guidance:** {answers.document_guidance.strip() or DEFAULT_DOCUMENT_GUIDANCE}",
                f"- **Input language selection:** {_input_language_text(answers, labels)}",
                "",
            ]
        )
    elif answers.create_source == CREATE_SOURCE_EXISTING:
        lines.extend(
            [
                "### Input Data & Source Language Strategy",
                f"- **Sources to inspect:** {SOURCE_LABELS[SOURCE_METADATA]}",
                f"- **Input language selection:** {_input_language_text(answers, labels)}",
                "",
            ]
        )
    lines.extend(
        [
            "### Multilingual Output Rules",
            f"- **Output language behavior:** {_output_language_text(answers, labels)}",
            "- *Note: Inspect each target property's definition in Uwazi. A translatable text property "
            "takes one value per output language; a shared property (select, multiselect, relationship, "
            "date, numeric, geolocation) takes a single value.*",
            "",
            "### Custom Instructions & Few-Shot Examples",
            f"- **Additional Rules:** {answers.additional_rules.strip() or 'None'}",
            f"- **Examples:** {answers.examples.strip() or 'None'}",
            "",
            "### Output Requirement",
            "Record each new entity as a CREATE suggestion (`create_entities`) to populate the user review "
            "table (do not create entities in Uwazi directly).",
        ]
    )
    return "\n".join(lines).strip()


def assemble_prompt(answers: GuidedAnswers, language_labels: Mapping[str, str] | None = None) -> str:
    """Render the answers as the prompt the generation LLM receives (pure).

    ``language_labels`` maps configured language keys to their labels (as read
    from the instance) so the prompt names a language the way the operator sees
    it while also carrying the key the script must write. An unknown key is
    rendered alone — the prompt never invents a label.
    """
    labels: Mapping[str, str] = language_labels or {}
    if answers.goal == GOAL_CREATE:
        return _assemble_create_prompt(answers, labels)
    templates = ", ".join(name.strip() for name in answers.templates if name.strip()) or "All templates"
    guidance = answers.document_guidance.strip() or DEFAULT_DOCUMENT_GUIDANCE

    lines: list[str] = [
        "### Task Goal",
        f"- **Flow:** {GOAL_DESCRIPTIONS.get(answers.goal, answers.goal)}",
        f"- **Target Templates:** {templates}",
        f"- **Entity Scope:** {_scope_text(answers)}",
        "",
        "### Input Data & Source Language Strategy",
        f"- **Sources to inspect:** {_sources_text(answers)}",
        f"- **Document reading guidance:** {guidance}",
        f"- **Input language selection:** {_input_language_text(answers, labels)}",
        "",
        "### Target Property & Logic",
        *_logic_lines(answers),
        "",
        "### Multilingual Output Rules",
        f"- **Output language behavior:** {_output_language_text(answers, labels)}",
        "- *Note: Inspect the target property definition in Uwazi. If it is a translatable text "
        "property, generate language-specific suggestions per the rule above. If it is a shared "
        "property (select, multiselect, relationship, date, numeric, geolocation), generate a "
        "single unified suggestion.*",
        "",
        "### Custom Instructions & Few-Shot Examples",
        f"- **Additional Rules:** {answers.additional_rules.strip() or 'None'}",
        f"- **Examples:** {answers.examples.strip() or 'None'}",
        "",
        "### Output Requirement",
        "Generate suggestions to populate the user review table (do not mutate Uwazi entities directly).",
    ]
    return "\n".join(lines).strip()


__all__ = [
    "CLASSIFY_TARGET_PROPERTY_TYPES",
    "DEFAULT_DOCUMENT_GUIDANCE",
    "EXISTING_CUSTOM",
    "EXISTING_MODES",
    "EXISTING_REPLACE",
    "EXISTING_SKIP",
    "EXISTING_VALUE_LABELS",
    "FALLBACK_BLANK",
    "FALLBACK_LABELS",
    "FALLBACK_MODES",
    "FALLBACK_VALUE",
    "CREATE_SOURCES",
    "CREATE_SOURCE_DESCRIPTIONS",
    "CREATE_SOURCE_DOCUMENTS",
    "CREATE_SOURCE_EXISTING",
    "CREATE_SOURCE_LABELS",
    "CREATE_SOURCE_UNRELATED",
    "FILL_TARGET_PROPERTY_TYPES",
    "GOALS",
    "GOAL_CREATE",
    "GOAL_CUSTOM",
    "GOAL_DESCRIPTIONS",
    "GOAL_FILL",
    "GOAL_LABELS",
    "GuidedAnswers",
    "INPUT_LANGUAGE_ANY",
    "INPUT_LANGUAGE_LABELS",
    "INPUT_LANGUAGE_MATCH",
    "INPUT_LANGUAGE_MODES",
    "INPUT_LANGUAGE_SPECIFIC",
    "OUTPUT_ALL",
    "OUTPUT_LANGUAGE_LABELS",
    "OUTPUT_LANGUAGE_MODES",
    "OUTPUT_SOURCE",
    "OUTPUT_SPECIFIC",
    "SCOPES",
    "SCOPE_ALL",
    "SCOPE_EMPTY",
    "SCOPE_FILTER",
    "SCOPE_LABELS",
    "SOURCES",
    "SOURCE_ATTACHMENTS",
    "SOURCE_LABELS",
    "SOURCE_METADATA",
    "SOURCE_PDF",
    "WizardLanguage",
    "WizardProperty",
    "WizardSchema",
    "WizardTemplate",
    "assemble_prompt",
    "fill_is_classification",
    "language_choices",
    "language_label_map",
    "metadata_property_choices",
    "property_choices",
    "property_type",
    "template_property_choices",
    "validate_answers",
]
