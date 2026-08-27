"""Dry-run a generated script against real entities with recorded writes.

Read-only extraction rehearsal: the exec namespace binds the REAL read helpers
(``query_entities`` / ``get_entity_files`` / ``get_file_bytes`` — live reads
against real entities and their supporting files) but every write helper is a
pure recorder (§ dry-run plan). The script runs end-to-end exactly as it would
in ``execute`` — extraction logic, the ``ctx`` contract, and the update-dict
build all execute — yet nothing is sent to Uwazi. The report shows per-op
would-be-write records plus the script's own ``result``; zero mutations.

Mirrors :class:`ExecuteScriptUseCase._exec`'s worker-thread pattern (a
dedicated event loop for the sync helpers' ``run_until_complete`` calls).
No manifest, no snapshots, no gate — dry runs never touch state, so there is
no state to gate.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace, run_script_sync
from uwazi_agent.ports.entity_api_port import EntityApiPort


class DryRunReport(BaseModel):
    """Outcome of a dry run: pass/fail, counters, and full per-op records."""

    passed: bool
    script_result: str | None
    script_error: str | None
    would_create: int
    would_update: int
    would_delete: int
    would_publish: int
    would_unpublish: int
    would_rewire: int
    records: list[dict[str, Any]]  # full per-op records; capped in rendering, not storage


class DryRunScriptUseCase:
    """Run a script against real entities with real reads and no-op writes."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        entity_repository: Any | None,
        file_repository: Any | None,
        default_language: str = "en",
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._entity_repository = entity_repository
        self._file_repository = file_repository
        self._default_language = default_language

    async def dry_run(self, script: str) -> DryRunReport:
        """Run ``script`` in the dry-run namespace; aggregate the records into a report."""
        return await asyncio.to_thread(self._dry_run_sync, script)

    def _dry_run_sync(self, script: str) -> DryRunReport:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            records: list[dict[str, Any]] = []
            namespace = build_dry_run_namespace(
                entity_api=self._entity_api,
                loop=loop,
                file_repository=self._file_repository,
                default_language=self._default_language,
                dry_run_records=records,
                entity_repository=self._entity_repository,
            )
            result, error = run_script_sync(script, namespace)
        finally:
            loop.close()
        counts = _count_ops(records)
        return DryRunReport(
            passed=error is None,
            script_result=result,
            script_error=error,
            would_create=counts["create"],
            would_update=counts["update"],
            would_delete=counts["delete"],
            would_publish=counts["publish"],
            would_unpublish=counts["publish:False"],
            would_rewire=counts["move_files"] + counts["create_relationships"],
            records=records,
        )


def _count_ops(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count records per op label; publish-ish ops split on their target state.

    ``set_publish_status`` records carry ``published``; ``publish`` /
    ``unpublish`` are counted as-is. The ``move_files`` + ``create_relationships``
    pair is reported together as ``would_rewire`` (merge/relationship rewiring).
    """
    counts: dict[str, int] = {}
    for r in records:
        op = str(r.get("op", ""))
        if op == "set_publish_status":
            key = "publish" if r.get("published") else "publish:False"
        elif op == "publish":
            key = "publish"
        elif op == "unpublish":
            key = "publish:False"
        else:
            key = op
        counts[key] = counts.get(key, 0) + 1
    for fallback in ("create", "update", "delete", "publish", "publish:False", "move_files", "create_relationships"):
        counts.setdefault(fallback, 0)
    return counts
