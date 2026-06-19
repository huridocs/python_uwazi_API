"""Isolated unit tests for the ``preview`` template property type AND
the ``style`` UI flag that is shared with ``image`` properties.

``preview`` is a TEMPLATE-ONLY decoration (no entity-side value) — the
Uwazi UI lets the user add it to a template to render an image carousel
in the entity view, but the user never edits a ``preview`` value on
individual entities.

The ``style`` field (``cover`` / ``fill`` / ``fit``) is shared between
``image`` and ``preview`` properties. The values the LLM sees match
the Uwazi UI labels (``fill`` = container-filling, ``fit`` =
containment without cropping), while the values Uwazi persists on disk
are ``cover`` / ``contain``. The mapper translates between the two:
``fill`` ↔ ``cover``, ``fit`` ↔ ``contain``. Other property types
ignore ``style``.

These tests cover the round-trip pieces that have to work for the type
to be wired in end-to-end:

1. ``PropertyType.PREVIEW`` and ``AgentPropertyType.PREVIEW`` exist and
   map to each other via the property-type mapper.
2. ``TemplateMapperAdapter.to_agent`` / ``to_api`` round-trip preview
   properties (and image properties) and their UI flags (``style``,
   ``fullWidth``), translating the agent-side ``fill``/``fit`` to the
   wire-side ``cover``/``contain`` and back.
3. ``Template.get_schema`` renders the preview type with a "no entity
   value" note (so the LLM does not look for one in entity metadata).
4. ``AGENT_PROPERTY_TYPE_FORMATS`` carries a preview entry so the LLM
   gets told preview is template-only.
5. ``PropertyStyle`` (wire) and ``AgentPropertyStyle`` (LLM-facing)
   enums each accept only their documented values and reject unknown
   ones (typo protection at the boundary).
"""

from pydantic import ValidationError

from uwazi_agent.adapters.property_type_mapper import (
    agent_to_api_property_type,
    api_to_agent_property_type,
)
from uwazi_agent.adapters.template_mapper import TemplateMapperAdapter
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_property_style import AgentPropertyStyle
from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_property_type_formats import AGENT_PROPERTY_TYPE_FORMATS
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_api.domain.property_schema import PropertySchema, PropertyStyle
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template


def test_preview_enum_exists_on_both_sides():
    assert PropertyType.PREVIEW.value == "preview"
    assert AgentPropertyType.PREVIEW.value == "preview"


def test_preview_maps_agent_to_api():
    assert agent_to_api_property_type(AgentPropertyType.PREVIEW) == PropertyType.PREVIEW


def test_preview_maps_api_to_agent():
    assert api_to_agent_property_type(PropertyType.PREVIEW) == AgentPropertyType.PREVIEW


def test_preview_format_instructions_mark_template_only():
    fmt = AGENT_PROPERTY_TYPE_FORMATS.get(AgentPropertyType.PREVIEW)
    assert fmt is not None
    assert "TEMPLATE-ONLY" in fmt
    assert "never" in fmt.lower()


def test_preview_format_instructions_explain_primary_document():
    """The LLM-facing format instructions must explain that preview
    renders the entity's PRIMARY document as an image (not a curated
    gallery of arbitrary images)."""
    fmt = AGENT_PROPERTY_TYPE_FORMATS[AgentPropertyType.PREVIEW]
    assert "PRIMARY document" in fmt
    assert "auto-generat" in fmt.lower()
    assert "never upload" in fmt.lower() or "never uploads" in fmt.lower()


def test_templates_instructions_explain_primary_document():
    """The templates-agent instructions must also explain that preview
    is the rendered primary document, so the agent creates preview
    properties for templates whose entities have a primary file."""
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    prose = TEMPLATES_INSTRUCTIONS
    assert "PRIMARY document" in prose
    assert "auto-generated" in prose
    assert "show_in_card" in prose  # practical guidance for typical setup


def test_entity_instructions_explain_primary_document():
    from uwazi_agent.use_cases.instructions.entity_instructions import (
        ENTITY_INSTRUCTIONS,
    )

    prose = ENTITY_INSTRUCTIONS
    assert "PRIMARY document" in prose
    assert "auto-generat" in prose.lower()


def test_python_instructions_explain_primary_document():
    from uwazi_agent.use_cases.instructions.python_instructions import (
        build_python_instructions,
    )

    prose = build_python_instructions()
    assert "PRIMARY document" in prose
    assert "auto-generat" in prose.lower()


# ---------------------------------------------------------------------------
# Discoverability regression tests (tiers 1, 2, 3)
# ---------------------------------------------------------------------------
# A simple user prompt like "add preview to template books" used to make the
# agent ask for clarification (text/markdown Preview label? set show_in_card
# on existing props?) instead of picking ``type='preview'``. The fix was to
# make ``preview`` discoverable as a peer of every other property type in
# three places the LLM reads:
#   1. ``TEMPLATES_INSTRUCTIONS`` — explicit "Supported property types" list.
#   2. ``create_template`` / ``update_template`` docstrings — they point at
#      the supported-types list and enumerate notable types.
#   3. The ``AgentProperty.type`` JSON schema — it now carries a description
#      that lists every value, with ``preview`` called out.
# These tests pin those three places so a future prose / schema edit cannot
# silently regress discoverability.


def test_templates_instructions_enumerates_every_supported_type():
    """The templates-agent prose must list every ``AgentPropertyType`` as
    a backticked token so the LLM can pattern-match user prompts against
    a complete list. ``preview`` must be a peer of every other type."""
    from uwazi_agent.domain.agent_property_type import AgentPropertyType
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    prose = TEMPLATES_INSTRUCTIONS
    for member in AgentPropertyType:
        token = f"``{member.value}``"
        assert token in prose, (
            f"AgentPropertyType.{member.name} ({member.value!r}) is missing "
            f"from TEMPLATES_INSTRUCTIONS as {token}; the agent cannot "
            f"discover it as a valid ``type`` value."
        )


def test_templates_instructions_has_supported_types_section_header():
    """The list of types lives under a clear "Supported property types"
    header that both the LLM and human readers can navigate to."""
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    assert "Supported property types" in TEMPLATES_INSTRUCTIONS


def test_templates_instructions_preview_is_a_peer_in_the_types_list():
    """The literal token ``preview`` must appear in the supported-types
    list context — not only in a dedicated paragraph below — so the LLM
    pattern-matches it as a valid ``type`` value."""
    from uwazi_agent.use_cases.instructions.templates_instructions import (
        TEMPLATES_INSTRUCTIONS,
    )

    prose = TEMPLATES_INSTRUCTIONS
    # Find the "Supported property types" section and confirm ``preview``
    # is enumerated there (not only in the dedicated paragraph further
    # down).
    start = prose.index("Supported property types")
    # Look at the next ~1200 chars; preview's enumeration row is there.
    section = prose[start : start + 1500]
    assert "``preview``" in section


def test_create_template_docstring_lists_preview_as_a_supported_type():
    """The create_template tool docstring must surface ``preview`` as one
    of the notable supported types so the LLM picks it instead of asking
    for clarification."""
    from uwazi_agent.use_cases.tools.create_template import create_template

    doc = create_template.__doc__ or ""
    assert "preview" in doc.lower()
    assert "Supported property types" in doc or "AgentPropertyType" in doc


def test_update_template_docstring_enumerates_supported_types():
    """The update_template docstring must enumerate every supported type
    so the LLM does not need to call a separate tool just to find them."""
    from uwazi_agent.use_cases.tools.update_template import update_template

    doc = update_template.__doc__ or ""
    assert "preview" in doc.lower()
    # Spot-check a handful of other types — the docstring must list more
    # than just ``preview`` so the enumeration is the source of truth.
    for token in ("text", "markdown", "relationship", "image"):
        assert token in doc, f"update_template docstring missing {token!r}"


def test_agent_property_type_field_has_schema_description_with_preview():
    """The ``AgentProperty.type`` JSON schema must carry a description
    that mentions every supported type, with ``preview`` called out, so
    the LLM gets an in-schema anchor next to the enum."""
    from uwazi_agent.domain.agent_property import AgentProperty
    from uwazi_agent.domain.agent_property_type import AgentPropertyType

    schema = AgentProperty.model_json_schema()
    type_schema = schema["properties"]["type"]
    description = type_schema.get("description", "")
    assert description, "AgentProperty.type must have a JSON schema description"
    assert "preview" in description.lower()
    # Every other type should also appear so the description is a complete
    # guide for the LLM.
    for member in AgentPropertyType:
        assert member.value in description, (
            f"AgentProperty.type schema description is missing {member.value!r}; the LLM cannot discover it inline."
        )


def test_property_style_enum_includes_cover_and_contain():
    """Wire-side enum: Uwazi persists only ``cover`` (default) and
    ``contain`` on disk (see ``AbstractImageProperty`` in the Uwazi
    server code). The LLM-facing ``AgentPropertyStyle`` adds the
    UI labels ``fill`` / ``fit`` on top of these."""
    members = {m.value for m in PropertyStyle if m is not PropertyStyle.EMPTY}
    assert members == {"cover", "contain"}


def test_agent_property_style_enum_includes_cover_fill_fit():
    """LLM-facing enum: ``cover`` / ``fill`` / ``fit`` mirror the
    labels shown in the Uwazi template editor."""
    members = {m.value for m in AgentPropertyStyle}
    assert members == {"cover", "fill", "fit"}


def test_property_style_cover_is_default_member():
    """``COVER`` is the documented default for the wire-side ``style`` field."""
    assert PropertyStyle.COVER.value == "cover"


def test_agent_property_style_cover_is_default_member():
    """``COVER`` is the documented default for the LLM-facing ``style`` field."""
    assert AgentPropertyStyle.COVER.value == "cover"


def test_property_schema_rejects_unknown_style_value():
    """A typo on the wire (e.g. ``'Coverr'``, ``'fill'``) is rejected
    with a clear validation error — ``fill`` is the UI label, not a
    wire value."""
    with __import__("pytest").raises(ValidationError):
        PropertySchema(type=PropertyType.IMAGE, style="fill")


def test_agent_property_rejects_unknown_style_value():
    """A typo on the LLM-facing surface (e.g. ``'Coverr'``, ``'Fill-'``)
    is rejected with a clear validation error."""
    with __import__("pytest").raises(ValidationError):
        AgentProperty(
            name="cover_image",
            type=AgentPropertyType.IMAGE,
            style="Fill-",  # type: ignore[arg-type]
        )


def test_property_schema_accepts_cover_and_contain():
    cover = PropertySchema(type=PropertyType.IMAGE, style="cover")
    contain = PropertySchema(type=PropertyType.PREVIEW, style="contain")

    assert cover.style is PropertyStyle.COVER
    assert contain.style is PropertyStyle.CONTAIN


def test_agent_property_accepts_cover_fill_and_fit():
    cover = AgentProperty(name="cover_image", type=AgentPropertyType.IMAGE, style=AgentPropertyStyle.COVER)
    fill = AgentProperty(name="cover_image", type=AgentPropertyType.IMAGE, style=AgentPropertyStyle.FILL)
    fit = AgentProperty(name="cover_image", type=AgentPropertyType.IMAGE, style=AgentPropertyStyle.FIT)

    assert cover.style is AgentPropertyStyle.COVER
    assert fill.style is AgentPropertyStyle.FILL
    assert fit.style is AgentPropertyStyle.FIT


def test_property_schema_normalises_empty_style_to_cover():
    """Legacy templates may carry ``style=""``; we normalise it to
    ``COVER`` (the documented default) so the LLM always sees a
    concrete style."""
    schema = PropertySchema.model_validate({"type": "image", "style": ""})
    assert schema.style is PropertyStyle.COVER


def test_property_schema_default_style_is_cover():
    """Omitting ``style`` on the wire defaults to ``COVER``."""
    schema = PropertySchema(type=PropertyType.TEXT)
    assert schema.style is PropertyStyle.COVER


def test_preview_round_trips_through_template_mapper_to_agent():
    """Wire ``contain`` must surface to the LLM as ``fit`` (the UI label)."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                _id="prop-id-1",
                name="preview_field",
                label="Preview",
                type=PropertyType.PREVIEW,
                noLabel=True,
                required=True,
                showInCard=True,
                style=PropertyStyle.CONTAIN,
                fullWidth=True,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert len(agent.properties) == 1
    prop = agent.properties[0]
    assert prop.name == "preview_field"
    assert prop.type == AgentPropertyType.PREVIEW
    assert prop.required is True
    assert prop.show_in_card is True
    assert prop.style is AgentPropertyStyle.FIT
    assert prop.full_width is True


def test_preview_round_trips_through_template_mapper_to_api():
    """Agent ``fit`` must serialise on the wire as ``contain``."""
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="preview_field",
                type=AgentPropertyType.PREVIEW,
                style=AgentPropertyStyle.FIT,
                full_width=True,
                show_in_card=True,
                required=False,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template)

    assert len(api_template.properties) == 1
    prop = api_template.properties[0]
    assert prop.name == "preview_field"
    assert prop.label == "preview_field"  # label defaults to name when no existing prop
    assert prop.type == PropertyType.PREVIEW
    assert prop.style is PropertyStyle.CONTAIN
    assert prop.fullWidth is True
    assert prop.showInCard is True
    assert prop.required is False


def test_image_property_round_trips_style():
    """``image`` properties carry a ``style`` flag; round-trip the
    translation between the agent-side ``fill``/``fit`` labels and the
    wire-side ``cover``/``contain`` values."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="cover",
                label="Cover",
                type=PropertyType.IMAGE,
                style=PropertyStyle.CONTAIN,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert agent.properties[0].style is AgentPropertyStyle.FIT

    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="cover",
                type=AgentPropertyType.IMAGE,
                style=AgentPropertyStyle.FILL,
            ),
        ],
    )
    api_template = mapper.to_api(agent_template)
    assert api_template.properties[0].style is PropertyStyle.COVER


def test_image_property_round_trips_cover_default():
    """An image property sent with the default ``style='cover'`` round-trips
    back as ``COVER``."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="cover",
                label="Cover",
                type=PropertyType.IMAGE,
                style=PropertyStyle.COVER,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert agent.properties[0].style is AgentPropertyStyle.COVER


def test_image_property_does_not_carry_full_width():
    """``fullWidth`` is preview-only; it must not be set on image props."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="cover",
                label="Cover",
                type=PropertyType.IMAGE,
                style=PropertyStyle.CONTAIN,
                fullWidth=True,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    prop = agent.properties[0]
    assert prop.style is AgentPropertyStyle.FIT
    assert prop.full_width is None


def test_preview_to_api_preserves_existing_style_and_full_width_when_omitted():
    """On update the agent may not re-send style/full_width; the mapper
    must preserve them from the existing template instead of blanking them."""
    existing = Template(
        name="Books",
        properties=[
            PropertySchema(
                _id="prop-id-1",
                name="preview_field",
                label="Preview",
                type=PropertyType.PREVIEW,
                style=PropertyStyle.CONTAIN,
                fullWidth=True,
            ),
        ],
    )
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="preview_field",
                label="Preview",
                type=AgentPropertyType.PREVIEW,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template, existing=existing)

    assert len(api_template.properties) == 1
    prop = api_template.properties[0]
    assert prop.style is PropertyStyle.CONTAIN
    assert prop.fullWidth is True


def test_preview_to_api_uses_agent_style_when_provided():
    """If the agent does send a style, it overrides the existing value."""
    existing = Template(
        name="Books",
        properties=[
            PropertySchema(
                _id="prop-id-1",
                name="preview_field",
                label="Preview",
                type=PropertyType.PREVIEW,
                style=PropertyStyle.CONTAIN,
                fullWidth=True,
            ),
        ],
    )
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="preview_field",
                label="Preview",
                type=AgentPropertyType.PREVIEW,
                style=AgentPropertyStyle.FILL,
                full_width=False,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template, existing=existing)

    prop = api_template.properties[0]
    assert prop.style is PropertyStyle.COVER
    assert prop.fullWidth is False


def test_preview_to_api_defaults_to_cover_when_agent_omits_and_no_existing():
    """On a fresh create where the agent doesn't specify ``style`` and
    there's no existing template, the mapper falls through to the
    API-side default (``COVER``)."""
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="preview_field",
                type=AgentPropertyType.PREVIEW,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template)

    assert api_template.properties[0].style is PropertyStyle.COVER


def test_template_get_schema_renders_preview_row():
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="preview_field",
                label="Preview",
                type=PropertyType.PREVIEW,
                style=PropertyStyle.COVER,
                fullWidth=True,
            ),
        ],
    )

    schema_md = api_template.get_schema(thesauri=[])

    assert "| Preview |" in schema_md
    assert "| preview |" in schema_md
    assert "no entity value" in schema_md


# ---------------------------------------------------------------------------
# Style-value translation tests — the mapper's only job where ``style`` is
# concerned is to translate between the agent-side UI labels
# (``cover`` / ``fill`` / ``fit``) and the wire-side Uwazi values
# (``cover`` / ``contain``). These tests pin that translation so a future
# edit to the mapper cannot silently regress it.
# ---------------------------------------------------------------------------


def test_mapper_translates_fill_to_cover_on_the_wire():
    """The LLM picks the UI label ``fill``; the mapper must rewrite it
    to the on-disk value ``cover`` before sending to Uwazi."""
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="cover",
                type=AgentPropertyType.IMAGE,
                style=AgentPropertyStyle.FILL,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template)

    assert api_template.properties[0].style is PropertyStyle.COVER


def test_mapper_translates_fit_to_contain_on_the_wire():
    """The LLM picks the UI label ``fit``; the mapper must rewrite it
    to the on-disk value ``contain`` before sending to Uwazi."""
    agent_template = AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(
                name="preview_field",
                type=AgentPropertyType.PREVIEW,
                style=AgentPropertyStyle.FIT,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    api_template = mapper.to_api(agent_template)

    assert api_template.properties[0].style is PropertyStyle.CONTAIN


def test_mapper_translates_contain_to_fit_on_read():
    """When Uwazi returns ``contain``, the mapper must surface the UI
    label ``fit`` to the LLM (and ``cover`` stays ``cover``)."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="preview_field",
                label="Preview",
                type=PropertyType.PREVIEW,
                style=PropertyStyle.CONTAIN,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert agent.properties[0].style is AgentPropertyStyle.FIT


def test_mapper_translates_cover_to_cover_on_read():
    """``cover`` is its own translation on either side."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="cover",
                label="Cover",
                type=PropertyType.IMAGE,
                style=PropertyStyle.COVER,
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert agent.properties[0].style is AgentPropertyStyle.COVER


def test_mapper_surfaces_legacy_empty_style_as_cover():
    """Legacy templates may carry ``style=""``. The api validator
    normalises that to ``COVER`` before the mapper sees it, so the
    LLM always sees a concrete agent-side ``COVER``."""
    api_template = Template(
        name="Books",
        properties=[
            PropertySchema(
                name="cover",
                label="Cover",
                type=PropertyType.IMAGE,
                style="",
            ),
        ],
    )

    mapper = TemplateMapperAdapter()
    agent = mapper.to_agent(api_template)

    assert agent.properties[0].style is AgentPropertyStyle.COVER
