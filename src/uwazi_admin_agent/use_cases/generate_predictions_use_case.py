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
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmLedger, LlmUsage
from uwazi_admin_agent.domain.review_refresh import ReconciliationSummary
from uwazi_admin_agent.domain.suggestion import (
    INFO_OP,
    PROPERTY_OPS,
    TITLE_PROPERTY,
    PredictionAttempt,
    PredictionAttemptStatus,
    Suggestion,
)
from uwazi_admin_agent.domain.suggestion_scoring import (
    compare_values,
    records_to_suggestions,
    value_in_language,
)
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.llm_runtime_port import LlmRuntimePort
from uwazi_admin_agent.ports.property_type_port import PropertyTypeLookupPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.suggestion_store_port import SuggestionStorePort
from uwazi_admin_agent.ports.template_label_port import TemplateLabelPort
from uwazi_admin_agent.ports.text_cache_port import TextCachePort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace, run_script_sync
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_agent.ports.entity_api_port import EntityApiPort


def _extract_property(raw: dict[str, Any], property_name: str, language: str | None = None) -> Any:
    """Pull a property's current value for ONE LANGUAGE out of a raw entity (pure).

    ``_title`` reads the raw's ``title``; any other name reads
    ``raw["metadata"][name]``, narrowed to ``language`` when the row is a
    per-language one (so a translated row compares against its own language's
    text, not the whole list). Both return None when absent — the "new value"
    case the accuracy score reports as neither match nor mismatch.
    """
    if property_name == TITLE_PROPERTY:
        return raw.get("title")
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return value_in_language(metadata.get(property_name), language)


def _fetch_raws(
    entity_repository: EntityRepositoryPort,
    pairs: set[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Fetch each ``(sharedId, language)`` raw once, batched, through the cached repository.

    A row must read ITS OWN language: Uwazi picks the entity-language row with
    the ``locale`` cookie, so fetching every entity with the default language
    made a Spanish suggestion compare against — and display — the English
    value. The pair is the cache key for the same reason.

    A failed entity/language is skipped (logged), never fatal: hydration is
    display and comparison context, and a row without it still records and
    still runs.
    """
    raws: dict[tuple[str, str], dict[str, Any]] = {}
    ordered = sorted(pairs)
    loop = asyncio.new_event_loop()
    try:
        for offset in range(0, len(ordered), EXECUTE_BATCH_SIZE):
            for shared_id, language in ordered[offset : offset + EXECUTE_BATCH_SIZE]:
                try:
                    raws[(shared_id, language)] = loop.run_until_complete(
                        entity_repository.get_raw_by_shared_id(shared_id, language)
                    )
                except Exception as exc:  # noqa: BLE001 — a missing entity is not fatal here
                    logger.warning("prediction hydration failed for shared_id={} language={}: {}", shared_id, language, exc)
    finally:
        loop.close()
    return raws


def _raw_language(suggestion: Suggestion, default_language: str) -> str:
    """The entity-language row a suggestion's current value lives on (pure)."""
    return suggestion.language or default_language


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


def _apply_hydration(
    suggestions: list[Suggestion],
    raws: dict[tuple[str, str], dict[str, Any]],
    default_language: str,
    template_labels: TemplateLabelPort | None = None,
) -> None:
    """Fill each row from the raw of ITS OWN entity-language pair (pure).

    Split out from :func:`hydrate_current_values` so the language pairing is
    testable without I/O: ``raws`` is keyed ``(sharedId, language)`` and a
    Spanish row reads the ``es`` raw. When Uwazi answers a missing language
    with its default row (the raw says ``language="en"``), that value belongs
    to another language and must not be shown as this row's current value — the
    row is a new translation, so ``current_value`` stays ``None``.
    """
    for index, suggestion in enumerate(suggestions):
        if suggestion.op == INFO_OP:
            # Display-only rows name no entity, so there is nothing to hydrate.
            continue
        if not suggestion.shared_id or suggestion.shared_id.startswith("dry-created-"):
            continue
        raw = raws.get((suggestion.shared_id, _raw_language(suggestion, default_language)))
        if raw is None:
            continue
        updates: dict[str, Any] = {}
        if suggestion.property_name is not None and suggestion.op in PROPERTY_OPS:
            raw_language = raw.get("language")
            answered_other_row = bool(
                suggestion.language and isinstance(raw_language, str) and raw_language != suggestion.language
            )
            current = None if answered_other_row else _extract_property(raw, suggestion.property_name, suggestion.language)
            updates["current_value"] = current
            updates["match"] = compare_values(suggestion.proposed_value, current)
        if not suggestion.entity_title:
            updates["entity_title"] = str(raw.get("title") or "")
        if not suggestion.template_name:
            updates["template_name"] = _template_label(template_labels, raw)
        if updates:
            suggestions[index] = suggestion.model_copy(update=updates)


def hydrate_current_values(
    entity_repository: EntityRepositoryPort | None,
    suggestions: list[Suggestion],
    default_language: str,
    template_labels: TemplateLabelPort | None = None,
) -> None:
    """Fill each row's before-state and its display identity from the live raws.

    Records carry no before-state, so the pass fetches the touched entities
    (batched, through the cached repository) — one fetch per entity-language
    pair — and reads the same key the row proposes IN THE ROW'S language.
    EVERY row that names a real entity is hydrated, not only the property ones:
    whole-entity records (``delete``/``publish``/``set_publish_status``/
    ``create_relationships``) carry no title and no template at all, so
    skipping them is what left the review table showing a bare sharedId and an
    empty Template column. Titles and template labels come from the raw (the
    template id resolved through ``template_labels``); a title/template the
    record already carried always wins — hydration only fills the gaps.
    Mutates the rows in place — they are about to be persisted. Module-level so
    other dry-run passes reuse the exact same hydration.
    """
    if entity_repository is None:
        return
    indexes = [
        index
        for index, s in enumerate(suggestions)
        if s.op != INFO_OP and s.shared_id and not s.shared_id.startswith("dry-created-")
    ]
    if not indexes:
        return
    pairs = {(suggestions[index].shared_id, _raw_language(suggestions[index], default_language)) for index in indexes}
    raws = _fetch_raws(entity_repository, pairs)
    _apply_hydration(suggestions, raws, default_language, template_labels)


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
        translation_port: TranslationPort | None = None,
        property_types: PropertyTypeLookupPort | None = None,
        llm_cache: TextCachePort | None = None,
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
        # The script helpers' translate_text rides this; None → loud unwired stub.
        self._translation_port = translation_port
        # The template property TYPES: what tells the recording path whether a
        # change is per-language (the free-text types) or one shared value.
        # None → the value's own shape decides, which agrees with the rule for
        # every type the write contract writes as a bare value.
        self._property_types = property_types
        # Content-addressed cache for the script's LLM calls: a refresh reuses
        # answers for unchanged inputs and pays nothing for the hits.
        self._llm_cache = llm_cache
        # Set by a reconcile refresh so the caller can report what changed.
        self.last_reconciliation: ReconciliationSummary | None = None

    def _property_types_for(self, records: list[dict[str, Any]]) -> dict[str, str]:
        """``{property_name: property_type}`` for the templates these records touch.

        The recording path needs the types to tell a per-language property from
        a shared one, and the templates come from the records themselves (the
        script names the template it writes to). One lookup per template, on
        the CACHED templates read the label lookup already performs — so this
        costs no extra request. No lookup wired, or a template that cannot be
        resolved, contributes nothing: the value's own shape decides then.
        """
        if self._property_types is None:
            return {}
        templates: list[str] = []
        for record in records:
            name = str(record.get("template_name") or "").strip()
            if name and name not in templates:
                templates.append(name)
        types: dict[str, str] = {}
        for template in templates:
            types.update(self._property_types.property_types(template))
        return types

    async def run(
        self,
        script: str,
        run_id: str,
        attempt: int,
        budget: LlmBudget,
        entity_scope: list[str] | None = None,
        *,
        reconcile: bool = False,
    ) -> PredictionAttempt:
        """Run one prediction pass and persist its suggestions.

        Returns the terminal attempt row (``COMPLETED``/``ABORTED``/``FAILED``).
        A budget abort does NOT raise: the aborted attempt row plus the kept
        suggestions ARE the user-facing result. Any other script error is
        recorded as ``FAILED`` and re-raised.

        ``reconcile=True`` is the in-place table refresh: the SAME attempt row is
        reused and the resulting rows are merged into the existing table by
        :meth:`SuggestionStorePort.reconcile_suggestions` (the pure rules live
        in :mod:`uwazi_admin_agent.domain.review_refresh`).
        """
        started = time.monotonic()
        scope = list(entity_scope) if entity_scope is not None else None
        now = datetime.now(timezone.utc)
        existing_attempt = self._store.get_attempt(run_id, attempt) if reconcile else None
        if reconcile and existing_attempt is None:
            raise PredictionPassError(f"cannot refresh run {run_id!r}: attempt {attempt} does not exist")
        attempt_row = (
            existing_attempt.model_copy(
                update={
                    "status": PredictionAttemptStatus.RUNNING,
                    "budget": budget,
                    "spent": LlmUsage(),
                    "entities_total": len(scope) if scope is not None else 0,
                    "entities_done": 0,
                    "entity_scope": scope,
                    "error": None,
                    "finished_at": None,
                }
            )
            if existing_attempt is not None
            else PredictionAttempt(
                run_id=run_id,
                attempt=attempt,
                status=PredictionAttemptStatus.RUNNING,
                budget=budget,
                entities_total=len(scope) if scope is not None else 0,
                entity_scope=scope,
                created_at=now,
            )
        )
        self._store.create_attempt(attempt_row)

        ledger = LlmLedger(budget)
        records: list[dict[str, Any]] = []
        _result, error, aborted = await asyncio.to_thread(self._run_script, script, records, ledger, run_id, attempt, scope)

        suggestions = records_to_suggestions(
            run_id,
            attempt,
            records,
            property_types=self._property_types_for(records),
        )
        if suggestions:
            await asyncio.to_thread(
                hydrate_current_values,
                self._entity_repository,
                suggestions,
                self._default_language,
                self._template_labels,
            )
        if reconcile:
            self.last_reconciliation = await asyncio.to_thread(
                self._store.reconcile_suggestions,
                run_id,
                attempt,
                suggestions,
            )
        elif suggestions:
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

    async def refresh(
        self,
        script: str,
        run_id: str,
        attempt: int,
        budget: LlmBudget,
    ) -> tuple[PredictionAttempt, ReconciliationSummary]:
        """Re-run the script against current Uwazi and merge into ``attempt``."""
        self.last_reconciliation = None
        final = await self.run(script, run_id=run_id, attempt=attempt, budget=budget, reconcile=True)
        return final, self.last_reconciliation or ReconciliationSummary()

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
                translation_port=self._translation_port,
                throttle=ThrottleController(),
                # apply_mode=True binds the REAL llm helpers when the runtime
                # is wired — the script's own llm_complete/llm_map calls, under
                # this pass's ledger. Without a runtime they are loud refusals.
                llm_runtime=self._llm_runtime,
                llm_ledger=ledger,
                llm_cache=self._llm_cache,
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
