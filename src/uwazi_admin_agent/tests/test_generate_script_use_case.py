"""Isolated unit tests for the script-generation agent wiring.

Per ``AGENTS.md``: no mocks/stubs, no network, no env creds. These tests exercise
pure construction only - ``Agent(..., model_settings=...)`` is lazy (it does not
contact any provider until ``run``), and ``_build_agent`` is a static factory, so
calling it with a string model name constructs the agent with no I/O.
"""

from uwazi_admin_agent.configuration import LLM_MAX_OUTPUT_TOKENS
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase


def test_build_agent_sets_max_tokens_model_settings() -> None:
    """The generation agent must raise the output token budget above the provider
    default so a long script + GeneratedScript JSON wrapping + reasoning fits.

    Without this, pydantic-ai aborts the final emit with
    ``UnexpectedModelBehavior: Model token limit (provider default) exceeded
    before any response was generated`` (intermittent - short scripts fit).
    """
    agent = GenerateScriptUseCase._build_agent("test")

    assert agent.model_settings is not None
    assert agent.model_settings.get("max_tokens") == LLM_MAX_OUTPUT_TOKENS


def test_build_agent_max_tokens_value_is_large_enough() -> None:
    """A multi-group merge script is ~100+ lines; 8192 is a sane floor. Pin it so
    a future edit can't silently shrink it below a useful output budget."""
    assert LLM_MAX_OUTPUT_TOKENS >= 4096
