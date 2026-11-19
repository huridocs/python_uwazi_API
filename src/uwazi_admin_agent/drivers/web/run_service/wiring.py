"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.configuration import (
    INSTANCE_IDS_DIR,
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
)
from uwazi_admin_agent.domain.instance_paths import InstancePaths, instance_paths
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmPrice
from uwazi_admin_agent.domain.run_name import validate_run_name


def _paths(url: str) -> InstancePaths:
    """The on-disk paths of ONE instance (every path this module writes)."""
    return instance_paths(url, INSTANCE_IDS_DIR)


def build_suggestion_store(url: str) -> SqliteSuggestionStore:
    """Return the shared SQLite suggestion store (schema ensured on build)."""
    return SqliteSuggestionStore(_paths(url).suggestions_db)


def read_run_script(url: str, run_id: str) -> str:
    """Read a run's script source (the ONE stable path the emitter writes).

    There is one script per run: ``script.py`` is overwritten when a
    regeneration produces a new one, so the live table always reflects the
    script that produced it. The id is validated first: it is a path segment,
    and a separator would step outside the instance's ``runs/`` tree.
    """
    run_id = validate_run_name(run_id)
    return (_paths(url).runs_path / run_id / "script.py").read_text(encoding="utf-8")


def _script_caps_budget() -> LlmBudget:
    """The configured default caps for the script's llm tool calls.

    Used when the wizard's budget wasn't passed (older callers/CLI): the same
    defaults the wizard's caps step prefills. The pass's ledger enforces them
    ONLY against the script's llm_complete/llm_map usage.
    """
    from uwazi_admin_agent.configuration import SCRIPT_DEFAULT_MAX_COST, SCRIPT_DEFAULT_MAX_REQUESTS

    return LlmBudget(
        max_cost=SCRIPT_DEFAULT_MAX_COST,
        max_requests=SCRIPT_DEFAULT_MAX_REQUESTS,
        price=LlmPrice(
            model="script-caps",
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        ),
    )
