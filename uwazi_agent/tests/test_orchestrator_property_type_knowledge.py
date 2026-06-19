"""Isolated unit tests for the orchestrator's template-property-type knowledge.

The orchestrator historically had no awareness of which template-property
``type`` values exist, so a user prompt like 'add a preview property to the
Books template' made it ask 'Markdown / Text / Link / Image / Media?' before
delegating — wasting a turn and forcing the user to spell out the obvious.
The fix is a 'Template property types — overview' block in the orchestrator
system prompt that names every type, gives clues that map user wording to
``type``, and pins ``preview`` as the unambiguous answer to 'preview'.

These tests pin the prose so a future edit cannot silently regress this
discoverability. Pure-string assertions, no mocks, no external services.
"""

from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.use_cases.instructions.orchestrator_instructions import (
    build_orchestrator_instructions,
)


def _orchestrator_prose() -> str:
    return build_orchestrator_instructions()


def test_orchestrator_has_property_types_overview_section():
    """The orchestrator must have a section that explains the property
    types it can delegate to the schema agent."""
    prose = _orchestrator_prose()
    assert "Template property types" in prose


def test_orchestrator_lists_every_supported_property_type():
    """Every ``AgentPropertyType`` value must appear as a backticked
    token in the orchestrator prose so the LLM can pattern-match a
    user prompt like 'add a preview property' against the canonical
    type list. ``preview`` is the most important one — it used to be
    missing entirely from the orchestrator's worldview."""
    prose = _orchestrator_prose()
    for member in AgentPropertyType:
        token = f"``{member.value}``"
        assert token in prose, (
            f"AgentPropertyType.{member.name} ({member.value!r}) is missing "
            f"from orchestrator instructions as {token}; the orchestrator "
            f"cannot discover it as a valid type."
        )


def test_orchestrator_preview_entry_is_marked_template_only():
    """The orchestrator's overview must call out that ``preview`` is
    TEMPLATE-ONLY and that the user never uploads or sets preview
    values, so the orchestrator does not try to delegate entity-side
    CRUD for it."""
    prose = _orchestrator_prose()
    start = prose.index("``preview``")
    section = prose[start : start + 1500]
    assert "TEMPLATE-ONLY" in section
    assert "PRIMARY document" in section
    assert "never" in section.lower()


def test_orchestrator_tells_to_delegate_immediately_when_type_is_clear():
    """When the user wording clearly picks a type (e.g. 'preview',
    'buy link'), the orchestrator must delegate without asking the
    user to choose between markdown / text / link / image / media."""
    prose = _orchestrator_prose()
    assert "DELEGATE" in prose
    assert "IMMEDIATELY" in prose
    assert "schema agent" in prose
    assert "do NOT ask" in prose.lower() or "do not ask" in prose.lower()


def test_orchestrator_defaulting_rules_include_preview():
    """The 'preview' / 'cover' / 'hero' / 'document preview' wording
    must be mapped to ``preview`` explicitly so the orchestrator
    defaulting table agrees with the schema agent's definition of
    ``type='preview'``."""
    prose = _orchestrator_prose()
    assert "preview" in prose.lower()
    assert "``preview``" in prose
    # The literal word 'preview' must appear in a defaulting/clue rule,
    # not only in the type-list. Look for it inside the overview block.
    overview_start = prose.index("Template property types")
    overview = prose[overview_start:]
    # 'preview' appears multiple times — at least once as a backticked
    # token (in the type list) and once as a bare clue word (in the
    # defaulting rules and the clue column).
    assert overview.count("preview") >= 3


def test_orchestrator_distinguishes_preview_from_image():
    """A previous confusion: the orchestrator could pick 'image' for
    'cover image'. The prose must distinguish them so 'preview' /
    'document preview' / 'first page' map to ``preview`` while only
    the per-entity-uploadable meaning maps to ``image``."""
    prose = _orchestrator_prose()
    assert "cover image" in prose.lower()
    # The disambiguation rule must explicitly say cover-image means image
    # (per-entity upload) and only literal 'preview' means the preview type.
    assert "literal" in prose.lower() and "preview" in prose.lower()


def test_orchestrator_instructs_to_use_default_language_silently():
    """The orchestrator must NOT ask 'English or Spanish?' after
    delegating — pick the instance default and state it."""
    prose = _orchestrator_prose()
    assert "en" in prose
    assert "English or Spanish" in prose or "english or spanish" in prose.lower()


def test_orchestrator_built_with_default_config_renders_full_prose():
    """Sanity check: the builder produces a non-trivial system prompt
    that includes the new overview block, so the runtime path that
    uses ``ORCHESTRATOR_INSTRUCTIONS`` (an alias to the builder) is
    actually picking up our edits."""
    prose = build_orchestrator_instructions()
    assert isinstance(prose, str)
    assert len(prose) > 1000
    assert "Template property types" in prose
    assert "``preview``" in prose
