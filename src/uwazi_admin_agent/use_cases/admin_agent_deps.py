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

from pydantic import Field

from uwazi_admin_agent.configuration import MAX_VALIDATION_ATTEMPTS
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


class AdminAgentDeps(UwaziAgentToolsDependencies):
    """``UwaziAgentToolsDependencies`` + raw repository + validation attempt counter."""

    entity_repository: EntityRepositoryPort | None = Field(
        default=None,
        description="Raw entity repository for dummy snapshot/revert (§2.5). Set by the use case before the agent runs.",
    )
    validation_attempts: int = Field(default=0, description="How many validation runs the agent has performed this turn.")
    validation_limit: int = Field(default=MAX_VALIDATION_ATTEMPTS, description="Hard cap on validation runs per turn.")
