"""Static analysis of the script's LLM usage (pure).

The cost projection must show the operator a per-entity estimate BEFORE the
budgeted pass runs. The script's source is the only information available at
that point (no run has happened yet), so this module reads its text and counts
the LLM call sites a single script run makes (the planning pass runs it once
over all entities — per-entity writes are recorded, and the budgeted passes
bind the same helpers):

- every ``llm_complete(`` occurrence is one request per entity run;
- every ``llm_map(`` occurrence is AT LEAST one request per entity run (the
  helper batches items in :data:`LLM_MAP_BATCH_SIZE` chunks — statically the
  item count is unknown, so the count assumes the minimum one batch and the
  modal's note says more batches are possible).

The result is a documented ESTIMATE: the enforced caps approved in the modal
(the :class:`~uwazi_admin_agent.domain.llm_budget.LlmBudget`) are what actually
bound a pass, exactly as the cost dialog says. This analysis is only ever fed
the ONE script (the planning pass runs it dry per creation budget).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_COMPLETE: re.Pattern[str] = re.compile(r"\bllm_complete\s*\(")
_MAP: re.Pattern[str] = re.compile(r"\bllm_map\s*\(")


class LlmScriptUsage(BaseModel):
    """What one apply-script run (ONE entity) spends on the LLM (estimate)."""

    model_config = ConfigDict(frozen=True)

    llm_complete_sites: int = Field(ge=0, description="``llm_complete(`` call sites in the apply script.")
    llm_map_sites: int = Field(ge=0, description="``llm_map(`` call sites in the apply script.")

    @property
    def uses_llm(self) -> bool:
        """True when the apply script can reach the LLM at all."""
        return self.llm_complete_sites + self.llm_map_sites > 0

    @property
    def calls_per_entity(self) -> int:
        """Worst-case requests ONE apply-script run makes (the modal's projection unit).

        ``llm_complete`` contributes one request per site; ``llm_map`` contributes
        one per site (its minimum single batch) — extra batches for item lists
        longer than the configured map batch size are possible and not statically
        known, which the modal's note states.
        """
        return self.llm_complete_sites + self.llm_map_sites


def apply_script_llm_usage(apply_script: str) -> LlmScriptUsage:
    """Count the LLM call sites of an APPLY script's source (pure).

    Text-level counting: a call site inside a string literal or a comment counts
    too. That is the safe direction for a cost projection (it can only
    overestimate, never under-charge), and generated scripts are flat sandbox
    bodies without imports or class boilerplate, so false positives are rare.
    """
    return LlmScriptUsage(
        llm_complete_sites=len(_COMPLETE.findall(apply_script)),
        llm_map_sites=len(_MAP.findall(apply_script)),
    )


__all__ = ["LlmScriptUsage", "apply_script_llm_usage"]
