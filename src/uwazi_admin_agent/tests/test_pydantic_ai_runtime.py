"""Isolated unit tests for the pydantic-ai runtime's usage conversion (per AGENTS.md).

Real objects throughout: a real :class:`pydantic_ai.AgentRunResult` carrying a
real :class:`pydantic_ai.usage.RunUsage`, and the real :func:`_usage_of`
transform. No network, no model, no instance, no mocks.
"""

from pydantic_ai import AgentRunResult
from pydantic_ai._agent_graph import GraphAgentState
from pydantic_ai.usage import RunUsage

from uwazi_admin_agent.adapters.pydantic_ai_runtime import _usage_of
from uwazi_admin_agent.domain.llm_budget import LlmUsage


def _run_with(usage: RunUsage) -> AgentRunResult[str]:
    """A real run result whose accumulated usage is ``usage``."""
    state = GraphAgentState()
    state.usage = usage
    return AgentRunResult("output", _state=state)


def test_usage_of_reads_the_run_usage_property() -> None:
    """pydantic-ai 2.x exposes ``usage`` as a property, not a callable."""
    run = _run_with(RunUsage(requests=2, input_tokens=30, output_tokens=12))

    assert _usage_of(run) == LlmUsage(requests=2, input_tokens=30, output_tokens=12)


def test_usage_of_defaults_to_zero_for_a_fresh_run() -> None:
    """A run that made no request yet converts to an all-zero usage."""
    assert _usage_of(_run_with(RunUsage())) == LlmUsage()
