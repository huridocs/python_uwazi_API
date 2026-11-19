"""Does the recorded plan contain anything the operator can actually RUN?

This module answers exactly that question, and it is the ONE place the answer
lives. Three callers need it, for two different reasons:

- the planning pass (:class:`GeneratePredictionsUseCase`) must not commit a
  table nobody can act on;
- the review table must not *present* an empty plan as a finished one;
- the regeneration flow must not mark a prompt as "Run" when the run it
  produced has no rows.

The reported failure it exists for: a script recorded 343 ``update_entities``
operations, but every one of them proposed the value the entity ALREADY had, so
the no-op conversion turned them into display-only ``matched`` rows — and at the
time the ONLY surviving row was the script's
``add_info("N entities could not be processed")`` failure report. The operator
saw a single info row and concluded the run had done nothing.

Pure: literal record dicts / suggestion lists in, a verdict out. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from uwazi_admin_agent.domain.suggestion import DISPLAY_ONLY_OPS, INFO_OP, MATCHED_OP, SKIPPED_OP, Suggestion

#: The record ops that become a RUNNABLE review row — exactly the set
#: :func:`~uwazi_admin_agent.domain.suggestion_scoring.records_to_suggestions`
#: converts into :class:`~uwazi_admin_agent.domain.suggestion.Suggestion` rows.
#: ``delete_file``/``refuse_file`` are deliberately NOT here: the recording path
#: has no review row for a file-collection delete yet (only the dry-run report
#: counts those records), so a file-only plan would hand the operator an empty
#: table. :func:`plan_intent_problem` names that gap explicitly instead of
#: reporting it as a script that gave up.
TABLE_CHANGE_OPS: frozenset[str] = frozenset(
    {
        "update",
        "create",
        "delete",
        "publish",
        "unpublish",
        "set_publish_status",
        "create_relationships",
        "move_files",
    }
)

#: File-collection records: real work with no review row (see above).
FILE_RECORD_OPS: frozenset[str] = frozenset({"delete_file", "refuse_file"})


@dataclass(frozen=True)
class PlanOutcome:
    """What a recorded plan actually amounts to, in table terms (pure value).

    ``change_rows`` is the number of RUNNABLE rows that reach the table after
    hydration and the no-op filter — not the number of records the script
    emitted. ``unchanged_rows`` counts the property writes whose value Uwazi
    already had; those become display-only ``matched`` rows. ``skipped_rows``
    counts the per-entity skipped outcomes.
    """

    change_rows: int
    unchanged_rows: int
    info_rows: int
    file_rows: int
    matched_rows: int = 0
    skipped_rows: int = 0

    @property
    def has_runnable_work(self) -> bool:
        """True when at least one row of the table can be executed."""
        return self.change_rows > 0

    @property
    def only_dropped_no_ops(self) -> bool:
        """The plan proposed work, but every change was already in place.

        This is a SUCCESS, not a failure: the entities are up to date. With the
        ``matched`` op the table now carries that evidence per entity, so this
        property mainly distinguishes the all-no-op case for callers that still
        summarize it.
        """
        return self.change_rows == 0 and self.unchanged_rows > 0 and self.file_rows == 0


def _record_op(record: dict[str, Any]) -> str:
    return str(record.get("op") or "")


def plan_outcome(
    records: list[dict[str, Any]],
    suggestions: list[Suggestion] | None = None,
    *,
    info_rows: int | None = None,
) -> PlanOutcome:
    """Summarize a recorded plan (pure).

    ``suggestions`` — when given — is the FINAL row list (hydrated and
    no-op-filtered), so ``change_rows`` is what the operator will really see.
    Without it only the records are known and every recorded change op counts as
    a row, which is the pre-hydration view the intent gate uses.
    """
    file_rows = sum(1 for record in records if _record_op(record) in FILE_RECORD_OPS)
    recorded_changes = sum(1 for record in records if _record_op(record) in TABLE_CHANGE_OPS)
    recorded_info = sum(1 for record in records if _record_op(record) == INFO_OP)

    if suggestions is None:
        return PlanOutcome(
            change_rows=recorded_changes,
            unchanged_rows=0,
            info_rows=recorded_info,
            file_rows=file_rows,
            skipped_rows=sum(1 for record in records if _record_op(record) == SKIPPED_OP),
        )
    change_rows = sum(1 for row in suggestions if row.op not in DISPLAY_ONLY_OPS)
    info_parents = sum(1 for row in suggestions if row.op == INFO_OP and row.detail_key is None)
    matched_rows = sum(1 for row in suggestions if row.op == MATCHED_OP)
    return PlanOutcome(
        change_rows=change_rows,
        unchanged_rows=max(0, recorded_changes - change_rows),
        info_rows=info_parents if info_rows is None else info_rows,
        file_rows=file_rows,
        matched_rows=matched_rows,
        skipped_rows=sum(1 for row in suggestions if row.op == SKIPPED_OP),
    )


def plan_refusal(
    records: list[dict[str, Any]],
    suggestions: list[Suggestion] | None,
    plan_kind: str | None,
    script_result: str | None = None,
) -> str | None:
    """Why this plan must NOT be committed, or ``None`` when it may be (pure).

    Judged on the plan AS THE TABLE WILL SHOW IT — ``suggestions`` is the final,
    hydrated, no-op-filtered row list. That is the whole point: a script can
    record 343 correct ``update_entities`` operations and still leave the table
    empty, because every proposed value was already stored. A record-level check
    cannot see that (it passed the reported run), a rendered-row check can.

    The four outcomes:

    - a ``query`` plan that records writes is refused (unasked-for writes);
    - a ``changes`` plan with at least one runnable row is accepted;
    - a ``changes`` plan whose every change was ALREADY applied is accepted —
      each entity keeps a display-only ``matched`` row;
    - a ``changes`` plan whose entities were all skipped is accepted — each
      entity keeps a display-only ``skipped`` row with its reason;
    - a ``changes`` plan with no runnable row and no result evidence (only
      info rows, or nothing at all) is refused: the generation gave up, and
      committing its failure report as "the table" is what hid the problem.

    ``suggestions is None`` means "not hydrated yet": the verdict then falls back
    to the recorded operations, which can only catch the give-up case.
    """
    if plan_kind is None:
        return None
    kind = str(plan_kind).strip().lower()
    outcome = plan_outcome(records, suggestions)

    if kind == "query":
        if not outcome.has_runnable_work and outcome.file_rows == 0:
            return None
        return (
            "The generated script declared plan_kind='query' (read only) but recorded "
            f"{outcome.change_rows + outcome.file_rows} change operation(s). A query plan must use only "
            "the read helpers and report its answer with `add_info`; change rows under a query "
            "declaration are writes the operator never asked to review. Declare "
            "plan_kind='changes' if the prompt does want a change."
        )

    if outcome.has_runnable_work:
        return None

    if outcome.only_dropped_no_ops:
        # Every recorded change already matches Uwazi. Nothing to run, and
        # nothing wrong: the per-entity matched rows carry that evidence.
        return None

    if outcome.skipped_rows:
        # Every change was skipped because the extraction could not produce a
        # value. This is a legitimate per-entity result, not a failed script:
        # the skipped rows explain exactly what happened for each entity.
        return None

    if outcome.file_rows:
        return (
            "The generated script records FILE deletions but no change row the review table can run. "
            "The planning pass has no review row for a file-collection delete yet, so this plan would "
            "leave an empty table. Record the file work together with the entity operation it belongs "
            "to (a merge's move + source delete), or ask the operator to run the file cleanup as its own "
            "reviewed task."
        )

    info_rows = [record for record in records if _record_op(record) == INFO_OP]
    headlines = [str(record.get("title") or "").strip() for record in info_rows]
    headlines = [headline for headline in headlines if headline]
    detail = f"it recorded only {len(headlines)} info row(s)" if headlines else "it recorded no operation at all"
    message = (
        "The generated script declared plan_kind='changes' but " + detail + ". Info rows are "
        "display-only: they change nothing in Uwazi and cannot be run, so this is a failed "
        "generation, not a review table. A change plan must record ONE change per entity with "
        "`update_entities`/`update_entities_parallel`/`create_entities`/`delete_entities`/... . "
        "If the target set was genuinely EMPTY, inspect it with a plan_kind='query' prompt first "
        "(or widen the selection) — an empty change plan is rejected on purpose, because it is "
        "indistinguishable from a script that could not compute its values."
    )
    if headlines:
        message += " Info row(s): " + " | ".join(headlines[:5]) + (" …" if len(headlines) > 5 else "") + "."
    result = (script_result or "").strip()
    if result:
        message += f" The script's own result was: {result}"
    return message


__all__ = [
    "FILE_RECORD_OPS",
    "PlanOutcome",
    "TABLE_CHANGE_OPS",
    "plan_outcome",
    "plan_refusal",
]
