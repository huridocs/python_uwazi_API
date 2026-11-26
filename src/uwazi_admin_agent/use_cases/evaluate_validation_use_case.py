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

from uwazi_admin_agent.domain.extraction_eval import is_blank
from uwazi_admin_agent.domain.suggestion import MOVE_OP, SKIPPED_OP, Suggestion
from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
from uwazi_admin_agent.domain.validation import (
    ValidationScore,
    ValidationSet,
    predictions_from_records,
    score_validation,
    script_hash,
    split_label_key,
    values_match,
)
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.generate_predictions_use_case import hydrate_current_values
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
        """Run ``script`` over the instance and score it on the frozen entities."""
        if self._entity_api is None:
            raise RuntimeError("validation evaluation needs the entity API to run the script")
        started = time.monotonic()
        records = await asyncio.to_thread(self._run_script, script)
        held = set(validation.shared_ids)
        held_records = [record for record in records if _addresses(record, held)]
        # A `skipped` record IS the script failing to propose a value for a
        # held-out entity, i.e. a missing prediction. It is not a review-table
        # "excluded" outcome here: the row must read as the MEASURED property
        # with the frozen label beside an empty suggestion, and the skip reason
        # becomes the row's evidence text. So the record is not rendered as a
        # row; its detail feeds the placeholder emitted below.
        measured = [record for record in held_records if str(record.get("op") or "") != SKIPPED_OP]
        rows = [
            row
            for row in records_to_suggestions(
                validation.run_id,
                measured,
                property_types={validation.property_name: validation.property_type},
            )
            # The set scores ONE property: a held-out entity's other writes are
            # not evidence for it, and showing them under "validate" would
            # suggest the score judged them.
            if row.property_name == validation.property_name
        ]
        rows.extend(_missing_rows(rows, validation, _skip_evidence(held_records)))
        # Hydration fills the display identity (title, template) from Uwazi, the
        # same way the planning pass does — without it a validation row reads as
        # a bare sharedId. It also writes the LIVE current value, which the
        # frozen label must replace: the score is measured against the value
        # captured when the set was frozen, never against the model's own output
        # (or today's edit). So the labels are applied AFTER hydration, last.
        if self._entity_repository is not None:
            await asyncio.to_thread(hydrate_current_values, self._entity_repository, rows, self._default_language)
        self.last_rows = [_with_frozen_label(row, validation) for row in rows]
        predictions = predictions_from_records(held_records, validation.property_name)
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

    def _run_script(self, script: str) -> list[dict[str, Any]]:
        """Execute the script against the instance exactly as the planning pass does.

        NO entity allow-list. The old version restricted every read to the
        held-out ids, which broke the ordinary classify-by-relationship shape: a
        script that resolves a name against a catalog template (``STATE``, a
        thesaurus-like set) got an empty catalog and recorded ``skipped`` for
        every held-out entity, so its score was 0 for a script that works.

        The score must measure the script the operator will run: same reads,
        recorded writes. Only the rows that address a held-out entity are kept
        (and scored), so a full pass costs the same reads the planning pass
        already does and nothing is narrowed that the script itself can see.
        """
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
            )
            _result, error = run_script_sync(script, namespace)
        finally:
            loop.close()
        if error is not None:
            logger.warning("validation pass script error: {}", error)
        return records


def _addresses(record: dict[str, Any], held: set[str]) -> bool:
    """Whether one recorded write addresses a held-out entity (pure).

    A merge/move record names its sources, not its target: a holdout entity
    absorbed as a merge SOURCE is scored too, because the script's behaviour on
    that entity is what the set measures.
    """
    op = str(record.get("op") or "")
    if op == MOVE_OP:
        named = [str(record.get("to_shared_id") or "")]
        named.extend(str(source) for source in record.get("from_shared_ids") or [])
    else:
        named = [str(record.get("shared_id") or "")]
    return any(one in held for one in named if one)


def _skip_evidence(records: list[dict[str, Any]]) -> dict[str, str]:
    """The recorded skip detail per held-out entity (pure), as the row's evidence.

    The script's own reason for proposing nothing is the one thing the operator
    needs to judge a missing prediction: "no STATE entity matched …" is
    actionable, an empty suggested value is not.
    """
    evidence: dict[str, str] = {}
    for record in records:
        if str(record.get("op") or "") != SKIPPED_OP:
            continue
        shared_id = str(record.get("shared_id") or "")
        detail = record.get("detail")
        text = "" if detail is None else str(detail).strip()
        if shared_id and text:
            evidence[shared_id] = text
    return evidence


def _missing_rows(
    rows: list[Suggestion],
    validation: ValidationSet,
    evidence: dict[str, str] | None = None,
) -> list[Suggestion]:
    """One row per frozen label the script produced NO measured value for (pure).

    Without it a held-out entity the script silently skipped would vanish from
    the table while still counting as a wrong/missing prediction in the score —
    the operator would read a number they cannot trace to a row. The row carries
    the measured property and the frozen label, so the table shows the exact
    comparison the score made.

    The check is per MEASURED label, not per entity: a script that proposed
    another property for a held-out entity still proposed nothing for the one
    being scored, and that missing prediction needs its own row.
    """
    evidence = evidence or {}
    per_language = validation.is_per_language()
    measured = {
        (row.shared_id, row.language if per_language else None)
        for row in rows
        if row.property_name == validation.property_name
    }
    placeholder: list[Suggestion] = []
    for key, expected in validation.labels.items():
        if is_blank(expected):
            continue
        shared_id, language = split_label_key(key)
        if shared_id not in validation.shared_ids or (shared_id, language if per_language else None) in measured:
            continue
        row = Suggestion(
            run_id=validation.run_id,
            shared_id=shared_id,
            op="update",
            property_name=validation.property_name,
            language=language if per_language else None,
            proposed_value=None,
            context=evidence.get(shared_id),
        )
        placeholder.append(_with_frozen_label(row, validation))
    return placeholder


def _with_frozen_label(row: Suggestion, validation: ValidationSet) -> Suggestion:
    """A validation row whose ``current_value`` is Uwazi's FROZEN label (pure).

    The pass records only what the script proposed, so its ``current_value`` is
    empty. The review surfaces must show the label the score is measured
    against right beside the suggestion — otherwise the operator cannot tell
    whether the extraction is right, and the row would read as a proposal with
    no counterpart. The label is never re-read here: the set froze it.

    A LANGUAGE-INDEPENDENT property has one label per entity (the set froze it
    under the language it was sampled in, while the row carries no language),
    which is why the lookup goes through :meth:`ValidationSet.label_for` instead
    of the raw key. The verdict uses the score's own comparison rule
    (:func:`values_match`) so the Match column cannot disagree with the number.
    """
    label = validation.label_for(row.shared_id, row.language)
    update: dict[str, Any] = {"is_validation": True, "current_value": label}
    # A row with NO proposal is a missing prediction, not a wrong one, and an
    # entity the set holds no label for is not judged at all: both keep the
    # verdict None ("nothing to compare") so the table never paints a script
    # that proposed nothing as having proposed the wrong value.
    update["match"] = (
        None
        if is_blank(label) or is_blank(row.proposed_value)
        else values_match(label, row.proposed_value, validation.property_type)
    )
    return row.model_copy(update=update)


__all__ = ["ValidationEvaluationUseCase"]
