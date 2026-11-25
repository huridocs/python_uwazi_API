"""Score a run's script against its frozen validation set (no persistence here).

The evaluation is a SEPARATE planning pass over the holdout entities only:

- reads are real and the namespace is scoped to the held-out ids, so the script
  can only see the entities it is scored on;
- writes are recorded and discarded — nothing is applied, nothing is persisted
  as reviewable work;
- the labels are the values frozen with the set, never the live ones (a Run, a
  revert or an external edit would otherwise turn the score into a comparison
  against the model's own output);
- the metric is micro-F1 for a set-valued property, normalized accuracy
  otherwise.

An LLM-using script would spend money and produce a non-deterministic number, so
the namespace binds NO LLM helpers: the call is recorded as a refusal and the
affected rows count as missing. The score must be reproducible.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.suggestion import Suggestion
from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
from uwazi_admin_agent.domain.validation import (
    ValidationScore,
    ValidationSet,
    predictions_from_records,
    score_validation,
    script_hash,
)
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace, run_script_sync
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_agent.ports.entity_api_port import EntityApiPort


class ValidationEvaluationUseCase:
    """Run the frozen script over the holdout entities and score the result."""

    def __init__(
        self,
        entity_api: EntityApiPort | None,
        entity_repository: EntityRepositoryPort | None,
        file_repository: FileRepositoryPort | None,
        segmentation_repository: SegmentationRepositoryPort | None = None,
        url_fetcher: UrlFetcherPort | None = None,
        translation_port: TranslationPort | None = None,
        default_language: str = "en",
    ) -> None:
        self._entity_api = entity_api
        self._entity_repository = entity_repository
        self._file_repository = file_repository
        self._segmentation_repository = segmentation_repository
        self._url_fetcher = url_fetcher
        self._translation_port = translation_port
        self._default_language = default_language
        #: The reviewable rows the last evaluation produced (None until one runs).
        self.last_rows: list[Suggestion] | None = None

    async def evaluate(
        self,
        script: str,
        validation: ValidationSet,
        now: datetime | None = None,
    ) -> ValidationScore:
        """Run ``script`` over ``validation``'s entities and score it."""
        if self._entity_api is None:
            raise RuntimeError("validation evaluation needs the entity API to run the script")
        started = time.monotonic()
        records = await asyncio.to_thread(self._run_script, script, set(validation.shared_ids))
        self.last_rows = [
            row.model_copy(update={"is_validation": True})
            for row in records_to_suggestions(
                validation.run_id,
                records,
                property_types={validation.property_name: validation.property_type},
            )
        ]
        predictions = predictions_from_records(records, validation.property_name)
        score = score_validation(
            validation,
            predictions,
            script_hash=script_hash(script),
            now=now or datetime.now(timezone.utc),
        )
        logger.info(
            "validation score run={} version={} metric={} value={:.3f} judged={} in {:.1f}s",
            validation.run_id,
            validation.version,
            score.metric,
            score.value,
            score.judged,
            time.monotonic() - started,
        )
        return score

    def _run_script(self, script: str, scope: set[str]) -> list[dict[str, Any]]:
        """Execute the script in a namespace limited to the holdout ids (worker thread)."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        records: list[dict[str, Any]] = []
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
                # Deliberately no LLM helpers (apply_mode=False): the score must
                # be reproducible and must not spend the operator's allowance.
                entity_scope=scope,
            )
            _result, error = run_script_sync(script, namespace)
        finally:
            loop.close()
        if error is not None:
            logger.warning("validation pass script error: {}", error)
        return records


__all__ = ["ValidationEvaluationUseCase"]
