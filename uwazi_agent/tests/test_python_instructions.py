import pytest

from uwazi_agent import configuration
from uwazi_agent.use_cases.instructions import (
    ORCHESTRATOR_INSTRUCTIONS,
    PYTHON_INSTRUCTIONS,
)


def test_python_instructions_is_a_callable():
    """PYTHON_INSTRUCTIONS is a builder so the limit can be injected from config."""
    assert callable(PYTHON_INSTRUCTIONS)
    assert isinstance(PYTHON_INSTRUCTIONS(), str)


def test_orchestrator_instructions_is_a_callable():
    """ORCHESTRATOR_INSTRUCTIONS is a builder so the limit can be injected from config."""
    assert callable(ORCHESTRATOR_INSTRUCTIONS)
    assert isinstance(ORCHESTRATOR_INSTRUCTIONS(), str)


def test_default_limit_rendered_in_python_instructions():
    instructions = PYTHON_INSTRUCTIONS()
    expected = str(configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT)
    assert expected in instructions


def test_default_limit_rendered_in_orchestrator_instructions():
    instructions = ORCHESTRATOR_INSTRUCTIONS()
    expected = str(configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT)
    assert expected in instructions


def test_python_instructions_render_uses_explicit_limit(monkeypatch):
    monkeypatch.setattr(configuration, "PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT", 4321)
    rendered = PYTHON_INSTRUCTIONS()
    assert "4321" in rendered
    assert "2500" not in rendered


def test_orchestrator_instructions_render_uses_explicit_python_limit(monkeypatch):
    monkeypatch.setattr(configuration, "PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT", 4321)
    rendered = ORCHESTRATOR_INSTRUCTIONS()
    assert "4321" in rendered
    assert "2500" not in rendered


def test_explicit_limit_argument_overrides_config(monkeypatch):
    monkeypatch.setattr(configuration, "PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT", 999999)
    rendered = PYTHON_INSTRUCTIONS(limit=7777)
    # The prose header is "($limit-char budget)"; check that exact phrase.
    assert "(7777-char budget)" in rendered
    assert "(999999-char budget)" not in rendered


def test_python_instructions_tell_agent_to_strip_metadata():
    """The instructions must tell the agent to drop non-essential metadata
    from ``result`` so it stays within the cap. This is the user-facing
    guidance: project, don't dump."""
    instructions = PYTHON_INSTRUCTIONS()
    assert "Strip everything non-essential" in instructions
    assert "shared_id" in instructions  # the example mentions dropping it
    # the worked example must project to title + a single metadata field
    assert "e['title']" in instructions
    assert "e['metadata'].get('date'" in instructions


def test_orchestrator_instructions_mention_python_cap():
    instructions = ORCHESTRATOR_INSTRUCTIONS()
    assert "HARD-CAPPED" in instructions
    assert str(configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT) in instructions


def test_orchestrator_instructions_mention_request_budget():
    """The orchestrator must be told (advisory) about the shared request
    budget so it can plan around it."""
    instructions = ORCHESTRATOR_INSTRUCTIONS()
    assert "Request budget" in instructions
    assert "shares a single budget" in instructions
    assert "advisory" in instructions
    # the prose must point at the config knob so the value can be cross-checked
    assert "configuration.REQUEST_LIMIT" in instructions
    # and inject the current config value
    assert str(configuration.REQUEST_LIMIT) in instructions
