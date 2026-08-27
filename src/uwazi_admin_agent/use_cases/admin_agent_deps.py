"""The admin agent's pydantic-ai deps (Phase 3).

Extends :class:`UwaziAgentToolsDependencies` (reused from ``uwazi_agent``) with
the two things the validation tool needs that the parent doesn't carry: the raw
:class:`EntityRepositoryPort` (for snapshot/revert against the real instance)
and a validation-attempt counter (mirrors ``browser_agent``'s ``AgentDeps`` — a
hard backstop so the LLM cannot burn the whole ``MAX_LLM_CALLS`` budget looping
on ``run_validation_script``).

Subclassing (rather than editing the shared ``UwaziAgentToolsDependencies``)
keeps the admin agent's extension owned by the admin agent. The reused
``query_entities`` tool is typed ``RunContext[UwaziAgentToolsDependencies]``; at
runtime it receives an ``AdminAgentDeps`` and accesses only parent fields, which
is sound because ``AdminAgentDeps`` *is-a* ``UwaziAgentToolsDependencies``
(verified end-to-end with pydantic-ai's TestModel).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from uwazi_admin_agent.configuration import MAX_DRY_RUN_ATTEMPTS, MAX_VALIDATION_ATTEMPTS
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


class AdminAgentDeps(UwaziAgentToolsDependencies):
    """``UwaziAgentToolsDependencies`` + raw repository + validation attempt counter."""

    entity_repository: EntityRepositoryPort | None = Field(
        default=None,
        description="Raw entity repository for dummy snapshot/revert (§2.5). Set by the use case before the agent runs.",
    )
    search_probe: SearchProbePort | None = Field(
        default=None,
        description=(
            "ES search probe for the dummy-gate settle (Option A). When None the "
            "harness skips the ES-visibility settle (backward-compatible; tests stay green)."
        ),
    )
    validation_attempts: int = Field(default=0, description="How many validation runs the agent has performed this turn.")
    validation_limit: int = Field(default=MAX_VALIDATION_ATTEMPTS, description="Hard cap on validation runs per turn.")
    file_repository: FileRepositoryPort | None = Field(
        default=None,
        description=(
            "Raw file repository for generation-time HTML sampling (peek_file_text) "
            "and the bound get_file_bytes helper. Set by build_runtime."
        ),
    )
    dry_run_use_case: Any | None = Field(
        default=None,
        description=(
            "DryRunScriptUseCase wired by build_runtime. The run_dry_run_script "
            "tool calls it to rehearse a candidate script against REAL entities "
            "with recorded (no-op) writes — real reads, zero mutations."
        ),
    )
    dry_run_attempts: int = Field(default=0, description="How many dry runs the agent has performed this turn.")
    dry_run_limit: int = Field(default=MAX_DRY_RUN_ATTEMPTS, description="Hard cap on dry runs per turn.")
    extractor_agent: Any | None = Field(
        default=None,
        description=(
            "The extractor subagent (pydantic-ai Agent), built once by "
            "GenerateScriptUseCase and called by the author_html_extractor tool."
        ),
    )
