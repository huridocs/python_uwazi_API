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

A generated script that produces its values with ``llm_complete``/``llm_map`` is
the normal shape of a classification task, so the evaluation binds those helpers
exactly as the planning pass does — under the run's approved budget and a fresh
ledger per evaluation. Refusing them made every LLM-driven row record ``skipped``
and scored a working script at 0.

The measured property's STORED value is hidden from the discovery reads (see
``hidden_measured_property``): the proposal must not be able to restate the very
value the score compares it against, which is what turned one validation sample
into evidence for a value the document never mentioned.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import (
    LLM_PRICE_INPUT_PER_MTOK,
    LLM_PRICE_OUTPUT_PER_MTOK,
)
from uwazi_admin_agent.domain.extraction_eval import is_blank
from uwazi_admin_agent.domain.llm_budget import LlmBudget, LlmLedger, LlmPrice
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
from uwazi_admin_agent.ports.llm_runtime_port import LlmRuntimePort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.template_label_port import TemplateLabelPort
from uwazi_admin_agent.ports.text_cache_port import TextCachePort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_admin_agent.use_cases.generate_predictions_use_case import hydrate_current_values
from uwazi_admin_agent.use_cases.script_exec_namespace import build_dry_run_namespace, run_script_sync
from uwazi_admin_agent.use_cases.script_exec_namespace.scope import hidden_measured_property
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
        llm_runtime: LlmRuntimePort | None = None,
        llm_cache: TextCachePort | None = None,
        llm_budget: LlmBudget | None = None,
        template_labels: TemplateLabelPort | None = None,
    ) -> None:
        self._entity_api = entity_api
        self._entity_repository = entity_repository
        self._file_repository = file_repository
        self._segmentation_repository = segmentation_repository
        self._url_fetcher = url_fetcher
        self._translation_port = translation_port
        self._default_language = default_language
        # The script's OWN llm_complete/llm_map calls. A generated script that
        # produces its values with the LLM is the normal shape of a
        # classification/extraction task; evaluating it with the helpers unbound
        # made every such call a refusal, so the row recorded `skipped` and the
        # performance score was 0 for a working script. Bound under the SAME
        # ledger contract the planning pass uses (the run's approved budget in,
        # a fresh ledger per evaluation), so the measured pass is the pass the
        # operator will run.
        self._llm_runtime = llm_runtime
        self._llm_cache = llm_cache
        self._llm_budget = llm_budget
        # Resolves a raw entity's template ID to its display name. Without it
        # hydration keeps the Mongo id, which is how a validation row read
        # ``5bfbb1a0…`` in the Template column after a sync.
        self._template_labels = template_labels
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
        records = await asyncio.to_thread(self._run_script, script, validation)
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
            await asyncio.to_thread(
                hydrate_current_values,
                self._entity_repository,
                rows,
                self._default_language,
                self._template_labels,
            )
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

    def _run_script(self, script: str, validation: ValidationSet) -> list[dict[str, Any]]:
        """Execute the script against the instance exactly as the planning pass does.

        The scored entities are what the pass must cover, so the namespace is
        scoped to them — see ``validation_evaluation_reads``. Restricting the
        whole instance (the old allow-list) broke the ordinary
        classify-by-relationship shape: a script that resolves a name against a
        catalog template (``STATE``, a thesaurus-like set) got an empty catalog
        and recorded ``skipped`` for every held-out entity, so its score was 0
        for a script that works. Restricting NOTHING broke the opposite way: the
        script discovered the whole instance, spent its budget there and could
        slice the sample away entirely.

        The LLM helpers are bound exactly as in the planning pass (a fresh
        ledger over the run's approved budget) whenever the runtime is wired: a
        script whose values come from ``llm_complete``/``llm_map`` is the normal
        shape of a classification task, and refusing those calls here recorded
        every row as ``skipped`` and scored 0 for a working script. A caller
        that wires no approved budget gets a DENY-ALL ceiling, so an
        LLM-driven script stops on its first call instead of spending
        unbudgeted money; either way the halt is reported and the rows recorded
        before it still count.

        The measured property's STORED value is hidden from the discovery reads
        (see ``hidden_measured_property``): the proposal must not be able to
        restate the very value the score compares it against, which is what
        turned one validation sample into evidence for a value the document
        never mentioned.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        records: list[dict[str, Any]] = []
        ledger = LlmLedger(self._llm_budget or _deny_all_budget())
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
                llm_runtime=self._llm_runtime,
                llm_ledger=ledger,
                llm_cache=self._llm_cache,
                apply_mode=self._llm_runtime is not None,
                hidden_property=hidden_measured_property(validation.property_name, validation.input_properties),
                evaluation_scope=set(validation.shared_ids),
                evaluation_templates=set(validation.template_names),
            )
            _result, error = run_script_sync(script, namespace)
        finally:
            loop.close()
        if error is not None:
            logger.warning("validation pass script error: {}", error)
        if ledger.exceeded:
            logger.warning("validation pass stopped at the script's LLM budget; the rows recorded before it count")
        return records


def _deny_all_budget() -> LlmBudget:
    """The ceiling an evaluation runs under when the caller passed no budget.

    Deny-by-default (both caps 0), exactly like the wizard's unchecked AI cap:
    a caller that wires no approved budget cannot make the evaluation spend
    money by accident. ``evaluate_run`` passes the run's own approved budget, so
    the measured pass is the pass the operator will run.
    """
    return LlmBudget(
        max_cost=0.0,
        max_requests=0,
        price=LlmPrice(
            model="script-caps",
            input_per_mtok=LLM_PRICE_INPUT_PER_MTOK,
            output_per_mtok=LLM_PRICE_OUTPUT_PER_MTOK,
        ),
    )


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
