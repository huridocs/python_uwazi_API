"""Run the PLANNING pass: real reads, recorded writes → the review table.

A planning pass is exactly a dry run (``build_dry_run_namespace``: real reads
against Uwazi, every write recorded and never sent) whose recorded would-be
writes are persisted as reviewable :class:`Suggestion` rows. Nothing here can
mutate Uwazi — the operator's Run action performs the writes later,
deterministically, under the backup intercept:

- **LLM helpers ARE bound**, under the budget the operator approved at task
  creation (the wizard's request cap + dollar cap). The generated script may
  call ``llm_complete``/``llm_map`` to compute values pure Python cannot; a
  breach halts the pass cleanly, keeping every row recorded before the halt
  (``ABORTED`` attempt status). When no budget is wired the helpers refuse
  loudly instead (scripts that avoid the LLM tool run identically).
- **Subset scoping.** A chosen ``entity_scope`` post-filters the discovery
  helpers, so the identical script plans over only those entities.
- **Current-value hydration.** Records carry no before-state, so the pass
  fetches the touched entities' raws afterwards and compares each proposed
  value against the live one — that comparison IS the accuracy score the
  review UI shows.

Progress is written to the attempt row as the pass runs, so the UI can poll
done/total while a 10 000-entity pass is in flight.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import EXECUTE_BATCH_SIZE
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmLedger
from uwazi_admin_agent.domain.suggestion import (
    TITLE_PROPERTY,
    PredictionAttempt,
    PredictionAttemptStatus,
    Suggestion,
)
from uwazi_admin_agent.domain.suggestion_scoring import compare_values, records_to_suggestions
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.template_label_port import TemplateLabelPort
from uwazi_admin_agent.ports.llm_runtime_port import LlmRuntimePort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace, run_script_sync
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_agent.ports.entity_api_port import EntityApiPort


def _extract_property(raw: dict[str, Any], property_name: str) -> Any:
    """Pull a property's current value out of a raw entity (pure).

    ``_title`` reads the raw's ``title``; any other name reads
    ``raw["metadata"][name]``. Both return None when absent — the "new value"
    case the accuracy score reports as neither match nor mismatch.
    """
    if property_name == TITLE_PROPERTY:
        return raw.get("title")
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata.get(property_name)


def _fetch_raws(
    entity_repository: EntityRepositoryPort,
    shared_ids: set[str],
    default_language: str,
) -> dict[str, dict[str, Any]]:
    """Fetch each entity's raw once, batched, through the cached repository.

    A failed entity is skipped (logged), never fatal: hydration is display and
    comparison context, and a row without it still records and still runs.
    """
    raws: dict[str, dict[str, Any]] = {}
    ordered = sorted(shared_ids)
    loop = asyncio.new_event_loop()
    try:
        for offset in range(0, len(ordered), EXECUTE_BATCH_SIZE):
            for shared_id in ordered[offset : offset + EXECUTE_BATCH_SIZE]:
                try:
                    raws[shared_id] = loop.run_until_complete(
                        entity_repository.get_raw_by_shared_id(shared_id, default_language)
                    )
                except Exception as exc:  # noqa: BLE001 — a missing entity is not fatal here
                    logger.warning("prediction hydration failed for shared_id={}: {}", shared_id, exc)
    finally:
        loop.close()
    return raws


def _template_label(
    template_labels: TemplateLabelPort | None,
    raw: dict[str, Any],
) -> str | None:
    """The entity's template LABEL — never the raw's template id (pure).

    A raw entity's ``template`` is the template's Mongo id ("5bfbb1a0…"), which
    is meaningless to an operator reading the review table. Resolving it through
    the templates list yields the name Uwazi displays ("MATTER"), which is also
    what the write payload expects (``get_by_name`` first). No lookup wired, or
    an unresolvable value ⇒ the raw value is kept rather than blanking the column.
    """
    value = str(raw.get("template") or raw.get("template_name") or "").strip()
    if not value:
        return None
    if template_labels is None:
        return value
    return template_labels.label_for(value) or value


def hydrate_current_values(
    entity_repository: EntityRepositoryPort | None,
    suggestions: list[Suggestion],
    default_language: str,
    template_labels: TemplateLabelPort | None = None,
) -> None:
    """Fill each row's before-state and its display identity from the live raws.

    Records carry no before-state, so the pass fetches the touched entities
    (batched, through the cached repository) and reads the same key the row
    proposes. EVERY row that names a real entity is hydrated, not only the
    property ones: whole-entity records (``delete``/``publish``/
    ``set_publish_status``/``create_relationships``) carry no title and no
    template at all, so skipping them is what left the review table showing a
    bare sharedId and an empty Template column. Titles and template labels come
    from the raw (the template id resolved through ``template_labels``); a
    title/template the record already carried always wins — hydration only fills
    the gaps. Mutates the rows in place — they are about to be persisted.
    Module-level so other dry-run passes reuse the exact same hydration.
    """
    if entity_repository is None:
        return
    indexes = [index for index, s in enumerate(suggestions) if s.shared_id and not s.shared_id.startswith("dry-created-")]
    if not indexes:
        return
    raws = _fetch_raws(entity_repository, {suggestions[index].shared_id for index in indexes}, default_language)
    for index in indexes:
        raw = raws.get(suggestions[index].shared_id)
        if raw is None:
            continue
        suggestion = suggestions[index]
        updates: dict[str, Any] = {}
        if suggestion.property_name is not None and suggestion.op in ("update", "create"):
            current = _extract_property(raw, suggestion.property_name)
            updates["current_value"] = current
            updates["match"] = compare_values(suggestion.proposed_value, current)
        if not suggestion.entity_title:
            updates["entity_title"] = str(raw.get("title") or "")
        if not suggestion.template_name:
            updates["template_name"] = _template_label(template_labels, raw)
        if updates:
            suggestions[index] = suggestion.model_copy(update=updates)


class GeneratePredictionsUseCase:
    """Produce reviewable suggestions from a script's recorded writes."""

    def __init__(
        self,
        entity_api: EntityApiPort | None,
        entity_repository: EntityRepositoryPort | None,
        file_repository: FileRepositoryPort | None,
        segmentation_repository: SegmentationRepositoryPort | None,
        url_fetcher: UrlFetcherPort | None,
        suggestion_store: SuggestionStorePort,
        llm_runtime: LlmRuntimePort | None = None,
        default_language: str = "en",
        template_labels: TemplateLabelPort | None = None,
    ) -> None:
        # The LLM runtime the script's llm_complete/llm_map helpers ride, under
        # the budgeted ledger built per pass below. None → loud refusal stubs.
        self._llm_runtime = llm_runtime
        self._entity_api = entity_api
        self._entity_repository = entity_repository
        self._file_repository = file_repository
        self._segmentation_repository = segmentation_repository
        self._url_fetcher = url_fetcher
        self._store: SuggestionStorePort = suggestion_store
        self._default_language = default_language
        self._template_labels = template_labels

    async def run(
        self,
        script: str,
        run_id: str,
        attempt: int,
        budget: LlmBudget,
        entity_scope: list[str] | None = None,
    ) -> PredictionAttempt:
        """Run one prediction pass and persist its suggestions.

        Returns the terminal attempt row (``COMPLETED``/``ABORTED``/``FAILED``).
        A budget abort does NOT raise: the aborted attempt row plus the kept
        suggestions ARE the user-facing result. Any other script error is
        recorded as ``FAILED`` and re-raised.
        """
        started = time.monotonic()
        scope = list(entity_scope) if entity_scope is not None else None
        attempt_row = PredictionAttempt(
            run_id=run_id,
            attempt=attempt,
            status=PredictionAttemptStatus.RUNNING,
            budget=budget,
            entities_total=len(scope) if scope is not None else 0,
            entity_scope=scope,
            created_at=datetime.now(timezone.utc),
        )
        self._store.create_attempt(attempt_row)

        ledger = LlmLedger(budget)
        records: list[dict[str, Any]] = []
        _result, error, aborted = await asyncio.to_thread(self._run_script, script, records, ledger, run_id, attempt, scope)

        suggestions = records_to_suggestions(run_id, attempt, records)
        if suggestions:
            await asyncio.to_thread(
                hydrate_current_values,
                self._entity_repository,
                suggestions,
                self._default_language,
                self._template_labels,
            )
            self._store.add_suggestions(suggestions)

        spent = ledger.snapshot()
        if aborted:
            logger.warning(
                "prediction pass ABORTED run={} attempt={} after {:.1f}s: {}",
                run_id,
                attempt,
                time.monotonic() - started,
                error,
            )
            final = attempt_row.model_copy(
                update={
                    "status": PredictionAttemptStatus.ABORTED,
                    "spent": spent,
                    "entities_done": len(scope) if scope is not None else len({s.shared_id for s in suggestions}),
                    "error": error,
                    "finished_at": datetime.now(timezone.utc),
                }
            )
            self._store.update_attempt(final)
            return final

        if error is not None:
            final = attempt_row.model_copy(
                update={
                    "status": PredictionAttemptStatus.FAILED,
                    "spent": spent,
                    "error": error,
                    "finished_at": datetime.now(timezone.utc),
                }
            )
            self._store.update_attempt(final)
            raise PredictionPassError(error)

        final = attempt_row.model_copy(
            update={
                "status": PredictionAttemptStatus.COMPLETED,
                "spent": spent,
                "entities_total": len({s.shared_id for s in suggestions}),
                "entities_done": len({s.shared_id for s in suggestions}),
                "finished_at": datetime.now(timezone.utc),
            }
        )
        self._store.update_attempt(final)
        logger.info(
            "prediction pass COMPLETED run={} attempt={} suggestions={} entities={} in {:.1f}s",
            run_id,
            attempt,
            len(suggestions),
            final.entities_total,
            time.monotonic() - started,
        )
        return final

    # --- internals -----------------------------------------------------------

    def _run_script(
        self,
        script: str,
        records: list[dict[str, Any]],
        ledger: LlmLedger,
        run_id: str,
        attempt: int,
        scope: list[str] | None,
    ) -> tuple[str | None, str | None, bool]:
        """Execute the script on a worker thread's own loop (the sandbox pattern).

        Returns ``(result, error, aborted)`` where ``aborted`` distinguishes a
        budget halt (safe, expected, kept) from any other script error.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        done = 0
        base_row = self._store.get_attempt(run_id, attempt) or PredictionAttempt(
            run_id=run_id, attempt=attempt, budget=ledger.budget
        )

        def progress(count: int) -> None:
            nonlocal done
            done += count
            self._store.update_attempt(base_row.model_copy(update={"entities_done": done}))

        try:
            namespace = build_dry_run_namespace(
                entity_api=self._entity_api,
                loop=loop,
                file_repository=self._file_repository,
                default_language=self._default_language,
                dry_run_records=records,
                entity_repository=self._entity_repository,
                segmentation_repository=self._segmentation_repository,
                url_fetcher=self._url_fetcher,
                throttle=ThrottleController(),
                # apply_mode=True binds the REAL llm helpers when the runtime
                # is wired — the script's own llm_complete/llm_map calls, under
                # this pass's ledger. Without a runtime they are loud refusals.
                llm_runtime=self._llm_runtime,
                llm_ledger=ledger,
                apply_mode=self._llm_runtime is not None,
                entity_scope=set(scope) if scope is not None else None,
            )
            result, error = run_script_sync(script, namespace)
        finally:
            loop.close()
        aborted = ledger.exceeded
        return result, error, aborted


class PredictionPassError(RuntimeError):
    """The prediction script failed for a reason other than the LLM budget."""


__all__ = [
    "GeneratePredictionsUseCase",
    "PredictionPassError",
    "hydrate_current_values",
]
