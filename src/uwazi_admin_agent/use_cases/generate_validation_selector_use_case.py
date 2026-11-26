"""Turn an operator's DESCRIPTION of a test set into a read-only selector script.

The wizard's custom test-set mode used to be a free-text search, which can only
express a title/text match. An operator needs property filters, date windows
and explicit shared ids. This use case asks the LLM for a tiny Python snippet
that calls the same read helpers a generated script uses and sets ``result``
(see :mod:`uwazi_admin_agent.domain.validation_selector`).

Not unit-tested (needs an LLM); the prompt/output contract and the result
normalization are pure and tested in ``test_validation_selector.py``.
"""

from __future__ import annotations

from pydantic_ai import Agent, UsageLimits
from pydantic_ai.models import Model

from uwazi_admin_agent.configuration import MAX_LLM_CALLS
from uwazi_admin_agent.domain.validation_selector import (
    SELECTOR_SYSTEM_PROMPT,
    ValidationSelector,
    build_selector_request,
)
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.clarify_schema_context import fetch_schema_context
from uwazi_agent.ports.llm_port import LlmPort


class GenerateValidationSelectorUseCase:
    """Ask the LLM for the selector snippet that freezes a run's test set."""

    def __init__(self, llm: LlmPort, deps: AdminAgentDeps | None = None) -> None:
        self._llm: LlmPort = llm
        # The instance's real template definitions: the snippet may only filter
        # on properties that exist (and are marked filterable).
        self._deps: AdminAgentDeps | None = deps

    @staticmethod
    def _build_agent(model: Model, system_prompt: str) -> Agent[None, ValidationSelector]:
        return Agent(model, system_prompt=system_prompt, output_type=ValidationSelector)

    async def execute(
        self,
        explanation: str,
        *,
        template_names: list[str],
        property_name: str,
        sample_size: int,
        language: str,
    ) -> ValidationSelector:
        """Write the selector for ``explanation`` (raises when the LLM fails)."""
        system_prompt = SELECTOR_SYSTEM_PROMPT.replace("{instance_schema}", await fetch_schema_context(self._deps))
        agent = self._build_agent(self._llm.get_model(), system_prompt)
        run = await agent.run(
            build_selector_request(
                explanation,
                template_names=template_names,
                property_name=property_name,
                sample_size=sample_size,
                language=language,
            ),
            usage_limits=UsageLimits(request_limit=MAX_LLM_CALLS),
        )
        output = run.output
        if not isinstance(output, ValidationSelector):
            raise RuntimeError(f"Agent returned an unsupported output type: {type(output).__name__}")
        return output


__all__ = ["GenerateValidationSelectorUseCase"]
