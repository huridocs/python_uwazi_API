"""Isolated unit tests for the ``nested`` template property type.

``nested`` is a TEMPLATE-ONLY parent group that gathers a repeatable
sub-set of OTHER properties under one parent key (Uwazi 'nested'). The
parent property itself carries no direct value — only its child
properties do. ``nested`` parents are an established Uwazi property
type (see ``uwazi/app/api/core/domain/template/PropertyType.ts`` and
``NestedProperty.ts`` upstream) and a number of legacy / existing
Uwazi instances have ``nested`` properties on their templates.

This module pins every layer that has to accept ``nested`` so the
read tools (``list_templates``, ``get_templates_by_names``,
``query_entities``) stop failing with a Pydantic
``ValidationError: Input should be 'text', 'date', 'select', ...``
whenever they encounter a real instance with nested properties:

1. ``PropertyType.NESTED`` and ``AgentPropertyType.NESTED`` exist and
   map to each other via the property-type mapper (so Pydantic accepts
   ``type='nested'`` on ``PropertySchema``).
2. ``TemplateMapperAdapter.to_agent`` round-trips nested properties
   (passing the parent through without crashing and never carrying a
   ``content`` / ``thesaurus`` coupling).
3. ``Template.get_schema`` renders the nested type row without
   crashing (the legacy value-format table now includes ``nested``).
4. ``AGENT_PROPERTY_TYPE_FORMATS`` carries a nested entry so the LLM
   gets told nested is template-only.
5. ``EntityMapper`` skips ``nested`` on both read and write
   (the parent never carries entity metadata).
6. The orchestrator and templates-agent prose enumerate ``nested``
   alongside every other type (regression pin via the
   ``AgentPropertyType`` enum iteration already used for preview).
7. ``PropertySchema`` does not raise on ``type='nested'`` — the
   regression case from the bug report.
"""

from uwazi_agent.adapters.property_type_mapper import (
    agent_to_api_property_type,
    api_to_agent_property_type,
)
from uwazi_agent.adapters.template_mapper import TemplateMapperAdapter
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_property_type_formats import AGENT_PROPERTY_TYPE_FORMATS
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template


# ---------------------------------------------------------------------------
# 1. Enum + mapper — the literal fix for the bug report's Pydantic error.
# ---------------------------------------------------------------------------


def test_nested_enum_exists_on_both_sides():
    assert PropertyType.NESTED.value == "nested"
    assert AgentPropertyType.NESTED.value == "nested"


def test_nested_maps_agent_to_api():
    assert agent_to_api_property_type(AgentPropertyType.NESTED) == PropertyType.NESTED


def test_nested_maps_api_to_agent():
    assert api_to_agent_property_type(PropertyType.NESTED) == AgentPropertyType.NESTED


def test_property_schema_accepts_nested_type():
    """The original bug: a real Uwazi instance returns ``type: "nested"``
    on a property and our Pydantic ``PropertySchema`` rejected it with
    ``Input should be 'text', 'date', 'select', ...``. This test pins
    the fix: ``type='nested'`` must construct without raising."""
    schema = PropertySchema(type="nested", name="birth", label="Birth")
    assert schema.type is PropertyType.NESTED


def test_property_schema_with_raw_nested_dict_does_not_raise():
    """The exact failure mode reported by the agent: Uwazi returns a
    property dict with ``type: "nested"`` and Pydantic
    ``model_validate`` used to crash. It must now succeed."""
    schema = PropertySchema.model_validate(
        {
            "name": "birth",
            "label": "Birth",
            "type": "nested",
        }
    )
    assert schema.type is PropertyType.NESTED


def test_agent_property_accepts_nested_type():
    """The agent-facing Pydantic model must also accept ``type='nested'``."""
    prop = AgentProperty(name="birth", type="nested")
    assert prop.type is AgentPropertyType.NESTED


# ---------------------------------------------------------------------------
# 2. Round-trip through the template mapper — read tools see ``nested``
#    cleanly without crashing or losing flags.
# ---------------------------------------------------------------------------


def test_nested_round_trips_through_template_mapper_to_agent():
    """A real Uwazi instance with a nested property on a template must
    survive ``TemplateMapperAdapter.to_agent`` so the read tools
    (``get_templates_by_names``, ``list_templates``) can return it."""
    api_template = Template(
        name="People",
        properties=[
            PropertySchema(name="title", label="Title", type=PropertyType.TEXT),
            PropertySchema(name="birth", label="Birth", type=PropertyType.NESTED),
            PropertySchema(name="nationality", label="Nationality", type=PropertyType.SELECT),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert len(agent.properties) == 3
    nested = next(p for p in agent.properties if p.name == "birth")
    assert nested.type is AgentPropertyType.NESTED


def test_nested_round_trips_through_template_mapper_to_api():
    """The mapper must accept ``type='nested'`` on the way out
    (``to_api``) so the schema agent can persist a nested property."""
    agent_template = AgentTemplate(
        name="People",
        properties=[
            AgentProperty(name="title", type=AgentPropertyType.TEXT),
            AgentProperty(name="birth", type=AgentPropertyType.NESTED),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template)

    assert len(api_template.properties) == 2
    nested = next(p for p in api_template.properties if p.name == "birth")
    assert nested.type is PropertyType.NESTED


# ---------------------------------------------------------------------------
# 3. Template.get_schema renders the nested row without crashing.
# ---------------------------------------------------------------------------


def test_template_get_schema_renders_nested_row():
    api_template = Template(
        name="People",
        properties=[
            PropertySchema(name="birth", label="Birth", type=PropertyType.NESTED),
        ],
    )

    schema_md = api_template.get_schema(thesauri=[])

    assert "| Birth |" in schema_md
    assert "| nested |" in schema_md
    assert "no entity value" in schema_md


# ---------------------------------------------------------------------------
# 4. AGENT_PROPERTY_TYPE_FORMATS carries a nested entry.
# ---------------------------------------------------------------------------


def test_nested_format_instructions_mark_template_only():
    """The LLM-facing format instructions must tell the agent that
    ``nested`` is template-only and carries no per-entity value, so
    the agent never tries to write a value for the parent key."""
    fmt = AGENT_PROPERTY_TYPE_FORMATS.get(AgentPropertyType.NESTED)
    assert fmt is not None
    assert "TEMPLATE-ONLY" in fmt
    assert "never" in fmt.lower()


def test_nested_format_instructions_explain_parent_role():
    """The instructions must explain that ``nested`` is a parent group
    for child properties, so the LLM does not treat it like a scalar
    field."""
    fmt = AGENT_PROPERTY_TYPE_FORMATS[AgentPropertyType.NESTED]
    assert "parent" in fmt.lower()
    assert "child" in fmt.lower()


# ---------------------------------------------------------------------------
# 5. Discoverability — every prose surface the LLM reads must list
#    ``nested`` as a peer of every other type.
# ---------------------------------------------------------------------------


def test_templates_instructions_enumerates_nested():
    """The templates-agent prose must list ``nested`` alongside every
    other property type so the LLM can pattern-match user prompts."""
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    prose = TEMPLATES_INSTRUCTIONS
    assert "``nested``" in prose


def test_templates_instructions_explains_nested_is_template_only():
    """The nested row must say ``TEMPLATE-ONLY`` so the agent does not
    try to send a value for a nested parent in any entity CRUD
    payload."""
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    prose = TEMPLATES_INSTRUCTIONS
    start = prose.index("``nested``")
    section = prose[start : start + 800]
    assert "TEMPLATE-ONLY" in section
    assert "entity CRUD payload" in section or "entity metadata" in section


def test_orchestrator_lists_nested_in_overview():
    """The orchestrator's property-type overview must mention
    ``nested`` so it can delegate without asking the user to spell
    out the type."""
    from uwazi_agent.use_cases.instructions.orchestrator_instructions import (
        build_orchestrator_instructions,
    )

    prose = build_orchestrator_instructions()
    assert "``nested``" in prose


def test_orchestrator_nested_entry_is_template_only():
    from uwazi_agent.use_cases.instructions.orchestrator_instructions import (
        build_orchestrator_instructions,
    )

    prose = build_orchestrator_instructions()
    start = prose.index("``nested``")
    section = prose[start : start + 800]
    assert "TEMPLATE-ONLY" in section


def test_entity_instructions_explain_nested_template_only():
    """The entity-agent prose must warn the LLM not to include a
    ``nested`` key in any entity payload."""
    from uwazi_agent.use_cases.instructions.entity_instructions import (
        ENTITY_INSTRUCTIONS,
    )

    prose = ENTITY_INSTRUCTIONS
    assert "nested" in prose
    # Find the nested entry and confirm it forbids entity-side writes.
    start = prose.index("``nested``")
    section = prose[start : start + 600]
    assert "TEMPLATE-ONLY" in section
    assert "Do NOT" in section or "do NOT" in section or "do not" in section.lower()


def test_python_instructions_explain_nested_template_only():
    from uwazi_agent.use_cases.instructions.python_instructions import (
        build_python_instructions,
    )

    prose = build_python_instructions()
    start = prose.index("``nested``")
    section = prose[start : start + 600]
    assert "TEMPLATE-ONLY" in section


# ---------------------------------------------------------------------------
# 6. AgentProperty.type JSON schema description enumerates ``nested``.
# ---------------------------------------------------------------------------


def test_agent_property_type_field_has_schema_description_with_nested():
    """The ``AgentProperty.type`` JSON schema description must mention
    ``nested`` so the LLM gets an in-schema anchor next to the enum."""
    schema = AgentProperty.model_json_schema()
    type_schema = schema["properties"]["type"]
    description = type_schema.get("description", "")
    assert "nested" in description


# ---------------------------------------------------------------------------
# 7. Create / update tool docstrings enumerate ``nested``.
# ---------------------------------------------------------------------------


def test_create_template_docstring_lists_nested_as_a_supported_type():
    from uwazi_agent.use_cases.tools.create_template import create_template

    doc = create_template.__doc__ or ""
    assert "nested" in doc


def test_update_template_docstring_enumerates_nested():
    from uwazi_agent.use_cases.tools.update_template import update_template

    doc = update_template.__doc__ or ""
    assert "nested" in doc


# ---------------------------------------------------------------------------
# 8. Entity-mapper skip — ``nested`` parents never carry entity
#    metadata (only their children do), so the mapper must skip them
#    on both read and write. We test the pure helper directly (it only
#    needs a ``Template``; no HTTP-backed repository is required).
# ---------------------------------------------------------------------------


def _make_mapper():
    from uwazi_api.use_cases.repositories.thesauri_repository import (
        ThesauriRepository,
    )
    from uwazi_agent.adapters.uwazi_api.entity_mapper import EntityMapper

    return EntityMapper(template_repo=None, thesauri_repo=ThesauriRepository())


def test_entity_mapper_skips_nested_on_read():
    """A real Uwazi payload that includes a ``nested`` parent's value
    must NOT propagate the parent key into the LLM-facing metadata —
    the parent is template-only."""
    template = Template(
        name="People",
        properties=[
            PropertySchema(name="title", label="Title", type=PropertyType.TEXT),
            PropertySchema(name="birth", label="Birth", type=PropertyType.NESTED),
        ],
    )

    mapper = _make_mapper()
    metadata = mapper._extract_agent_metadata(
        api_metadata={
            "title": [{"value": "Ada"}],
            # A nested parent — even if Uwazi stored something here in a
            # legacy instance, the LLM-facing mapper must drop it
            # because nested is template-only.
            "birth": [{"value": {"date": "1815-12-10"}}],
        },
        template=template,
        language="en",
    )

    assert "title" in metadata
    assert "birth" not in metadata


def test_entity_mapper_skips_nested_on_write():
    """If the agent accidentally sends a ``nested`` key in a write
    payload, the mapper must drop it (mirroring the read-side skip and
    preview's behaviour)."""
    template = Template(
        name="People",
        properties=[
            PropertySchema(name="title", label="Title", type=PropertyType.TEXT),
            PropertySchema(name="birth", label="Birth", type=PropertyType.NESTED),
        ],
    )

    mapper = _make_mapper()
    coerced = mapper._coerce_metadata(
        metadata={
            "title": "Ada",
            # The agent should not normally send this, but if it does,
            # the mapper must silently drop the nested key rather than
            # rejecting the whole entity.
            "birth": {"date": "1815-12-10"},
        },
        template=template,
        language="en",
    )

    assert "title" in coerced
    assert "birth" not in coerced


# ---------------------------------------------------------------------------
# 9. Entity validator — a nested parent never satisfies a required
#    property and never trips the value-shape validator.
# ---------------------------------------------------------------------------


def test_entity_validator_value_shape_accepts_nested():
    """The per-property value-shape validator must treat ``nested`` as
    template-only and pass any value through without complaint (mirror
    of the existing ``preview`` branch)."""
    from uwazi_api.use_cases.repositories.entity_validator import EntityValidator

    validator = EntityValidator()
    # The validator's value-shape check accepts any value when the
    # property type is template-only; calling it with an arbitrary
    # nested payload must not raise.
    validator._validate_property_value("birth", {"date": "1815-12-10"}, "nested")
    validator._validate_property_value("birth", None, "nested")
