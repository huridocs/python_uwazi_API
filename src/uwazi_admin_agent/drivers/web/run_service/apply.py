"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
)
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    ApplyRefusedError,
    ApplySuggestionsUseCase,
)

from .wiring import build_suggestion_store


def _apply_use_case(runtime: Any) -> ApplySuggestionsUseCase:
    """Wire the apply use case over a built runtime (the Run heuristic).

    No LLM wiring: the heuristic executes precomputed rows only — no script
    runs, no model calls, no budget to approve at write time.
    """
    return ApplySuggestionsUseCase(
        entity_api=runtime.entity_api,
        relationship_api=runtime.relationship_api,
        entity_repository=runtime.entity_repository,
        backup_store=runtime.backup_store,
        audit_log=runtime.audit_log,
        suggestion_store=build_suggestion_store(runtime.url),
        file_repository=runtime.file_repository,
        segmentation_repository=runtime.segmentation_repository,
        url_fetcher=runtime.url_fetcher,
        cache_control=runtime.file_cache,
        default_language=DUMMY_LANGUAGE,
        publish_state=getattr(runtime, "publish_state", None),
        property_types=getattr(runtime, "property_types", None),
        thesaurus_values=getattr(runtime, "thesaurus_values", None),
    )


async def apply_outcomes(
    url: str,
    run_id: str,
    user: str,
    password: str,
    selection: list[tuple[str, str | None]],
    row_ids: list[int] | None = None,
    progress: Any | None = None,
) -> list[str]:
    """Execute the selected entities' rows into Uwazi (revertable).

    The Run action: the deterministic heuristic over the precomputed rows —
    property rows into per-entity update payloads, creates into create
    payloads, delete/publish/unpublish/set_publish_status/relationship rows
    through their dedicated intercepted helpers. The writes land in the run's
    OWN accumulating manifest with pre-change snapshots: a Run adds no row to
    the tasks table, it only changes the suggestion statuses and the run's
    status, which is what lets one whole-task revert undo it.
    Returns the run ids touched (the run itself).

    The Run stands in for the row-level accept: the use case accepts the
    selected rows' PENDING state as it pushes them, so the rows the review
    table shows (recorded by the planning pass) are what gets written.
    ``selection`` is the table's checked (sharedId, language) rows, so a
    translation task writes only the languages the operator checked.
    ``row_ids`` narrows the Run to the checked SUBOPERATIONS (the table's
    per-property rows): running one property of one entity-language group
    writes exactly that property and leaves its siblings pending.
    ``progress`` is the optional ``(done, total)`` callback the gate's toast
    polls — the SUBOPERATION fraction the operator watches while it runs.

    Raises :class:`ApplyRefusedError` when there is nothing to run.
    """
    if not selection:
        raise ApplyRefusedError("nothing selected to run")

    def _work() -> list[str]:
        runtime = build_runtime(user=user, password=password, url=url)
        use_case = _apply_use_case(runtime)
        use_case.apply(run_id, selection, row_ids, progress=progress)
        return [run_id]

    return await asyncio.to_thread(_work)
