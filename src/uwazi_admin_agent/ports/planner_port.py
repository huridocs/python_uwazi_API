from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.plan import MigrationPlan


class PlannerPort(ABC):
    """Turn a natural-language prompt into a declarative MigrationPlan (§2.1, §5.3).

    The planner emits **only** a ``MigrationPlan`` - never code, never free-form
    instructions. That constraint is enforced by the adapter's system prompt
    (Phase 4), not by this signature.
    """

    @abstractmethod
    async def plan(self, prompt: str) -> MigrationPlan:
        """Produce a migration plan from an operator prompt."""
        ...
