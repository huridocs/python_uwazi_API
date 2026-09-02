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
import time
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.file_cache import FileCacheStats, format_cache_stats
from uwazi_admin_agent.ports.cache_stats_port import CacheStatsPort
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
    samples: list[dict[str, Any]] = Field(
        default_factory=list, description="First N would-be-update records, trimmed for LLM reading."
    )
    cache_stats: FileCacheStats | None = Field(
        default=None,
        description=(
            "Aggregate persistent-cache counters for THIS dry run (None when the "
            "cache is disabled/unwired): fetches vs hits for file bytes and raws."
        ),
    )


_SAMPLES_CAP: int = 20


class DryRunScriptUseCase:
    """Run a script against real entities with real reads and no-op writes."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        entity_repository: Any | None,
        file_repository: Any | None,
        default_language: str = "en",
        cache_stats: CacheStatsPort | None = None,
    ) -> None:
        self._entity_api: EntityApiPort = entity_api
        self._entity_repository = entity_repository
        self._file_repository = file_repository
        self._default_language = default_language
        self._cache_stats: CacheStatsPort | None = cache_stats

    async def dry_run(self, script: str) -> DryRunReport:
        """Run ``script`` in the dry-run namespace; aggregate the records into a report.

        Logs the wall-clock duration on completion (and the traceback on a
        crash): against a large remote instance the run's real reads can take
        many minutes, and this boundary line is what separates "slow" from
        "stuck" when nothing else logs during the script's execution.
        """
        started = time.monotonic()
        # Per-boundary cache window: only THIS dry run's reads count, so each of
        # the up-to-4 passes a generation turn can run reports its own line.
        if self._cache_stats is not None:
            self._cache_stats.reset_stats()
        try:
            report = await asyncio.to_thread(self._dry_run_sync, script)
        except Exception:  # noqa: BLE001 — re-raised; only the visibility changes
            logger.exception("dry run crashed after {:.1f}s", time.monotonic() - started)
            raise
        if self._cache_stats is not None:
            report.cache_stats = self._cache_stats.snapshot_stats()
        logger.info(
            "dry run finished in {:.1f}s passed={} script_error={} | cache: {}",
            time.monotonic() - started,
            report.passed,
            bool(report.script_error),
            format_cache_stats(report.cache_stats),
        )
        return report

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
            samples=_update_samples(records),
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


def _update_samples(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First N update records trimmed to (shared_id, metadata) for LLM reading."""
    samples = [{"shared_id": r.get("shared_id"), "metadata": r.get("metadata")} for r in records if r.get("op") == "update"]
    return samples[:_SAMPLES_CAP]
