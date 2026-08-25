"""Pure helpers for the ES-freshness settle before dummy cleanup (Option A, rev 2).

The dummy gate creates real dummies and deletes them rapidly. Uwazi indexes
creates/updates via ES ``bulk`` (awaited but NOT refreshed) and deletes via
``deleteByQuery(..., conflicts: 'proceed', refresh: true)`` (``search/search.js::
bulkDeleteBySharedId``). ``deleteByQuery`` snapshots the *refreshed* index and
deletes each doc by the snapshot's ``seq_no``; if a doc was re-indexed since the
snapshot (a newer unrefreshed version sits in the translog), the delete hits a
**version conflict** and ``conflicts: 'proceed'`` **skips it**. The trailing
``refresh: true`` then flushes that newer version into a segment -> the doc
reappears as an **orphan** (Mongo gone, ES still has it) -> the shared index
needs ``yarn reindex``.

The orphan arises whenever a re-index (the revert's ``save_raw``) lands in the
translog right before the cleanup delete. The fix has two parts:

* **(A) Skip the no-op revert** for an unchanged dummy (``before == after`` excl.
  platform-managed) so the revert never re-indexes in the first place.
* **(B) editDate-freshness settle** before the delete: ``editDate`` is bumped on
  every save (server-managed), so it is a monotonic "which version is refreshed"
  signal. Poll the ES doc's ``editDate`` until it reaches the latest Mongo
  ``editDate`` seen for that sharedId, so the ``deleteByQuery`` snapshots the
  latest version (no conflict) and removes it cleanly.

This module holds the **pure** pieces (no I/O): the ``/api/v2/search`` response
parser, the freshness-state assembler, the per-sharedId target builder, the
skip-revert decision, and the warning formatter. The I/O polling loop lives in
:class:`uwazi_admin_agent.use_cases.dummy_entity_harness.DummyEntityHarness.
_wait_for_es_fresh` (validated live, not unit-tested).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.validation_result import PLATFORM_MANAGED_FIELDS


def _to_int(value: Any) -> int | None:
    """Coerce an ES ``editDate`` (number or numeric string) to int; ``None`` if absent/unparseable."""
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; not a valid editDate
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def extract_edit_date(search_json: dict[str, Any]) -> int | None:
    """Return the ES doc's ``editDate`` from an ``/api/v2/search`` response, or ``None``.

    The probe calls ``GET /api/v2/search?filter[sharedId]=<id>&fields[]=sharedId
    &fields[]=editDate`` with the dummy-language filter, so ``data`` holds the
    single matching language row. Returns the max ``editDate`` across hits
    (defensive against an unfiltered response) so the caller can compare it to
    the latest Mongo ``editDate``. A missing/non-list ``data`` or hits without a
    parseable ``editDate`` yield ``None`` (not visible/not fresh -> keep polling).
    """
    data = search_json.get("data")
    if not isinstance(data, list):
        return None
    dates = [_to_int(hit.get("editDate")) for hit in data if isinstance(hit, dict)]
    present = [d for d in dates if d is not None]
    if not present:
        return None
    return max(present)


class FreshnessResult(BaseModel):
    """Outcome of waiting for each sharedId's ES ``editDate`` to reach its target.

    ``all_fresh`` is True iff every expected sharedId's ES ``editDate`` >= its
    target (the latest Mongo ``editDate`` seen for it) -> the ``deleteByQuery``
    will snapshot the latest version and remove it with no version conflict.
    ``timed_out`` is True iff the deadline elapsed before all were fresh; the
    harness then proceeds best-effort and records an ``es_settle_warning`` (gate
    correctness is Mongo-based, so a slow ES must not fail the validation).
    """

    model_config = ConfigDict(frozen=True)

    expected_ids: list[str] = Field(description="The sharedIds the settle waited for.")
    fresh_ids: list[str] = Field(description="The subset whose ES editDate reached target, in expected order.")
    pending_ids: list[str] = Field(description="The subset still not fresh when the settle ended, in expected order.")
    all_fresh: bool = Field(description="True iff every expected sharedId was fresh.")
    timed_out: bool = Field(description="True iff the deadline elapsed before all were fresh.")


def build_freshness_result(targets: dict[str, int], observed: dict[str, int | None], timed_out: bool) -> FreshnessResult:
    """Assemble a :class:`FreshnessResult` from the per-id targets and observed editDates.

    Pure: no I/O. An id is **fresh** iff its observed ``editDate`` is not ``None``
    and ``>=`` its target. A target of ``0`` reduces to "visible" (any positive
    editDate qualifies), so the post-create settle reuses this with target ``0``.
    Result lists preserve the ``targets`` insertion order for stable diagnostics.
    """
    expected = list(targets.keys())
    fresh = [
        sid
        for sid in expected
        if observed.get(sid) is not None and observed[sid] is not None and observed[sid] >= targets[sid]
    ]
    pending = [sid for sid in expected if sid not in fresh]
    return FreshnessResult(
        expected_ids=expected,
        fresh_ids=fresh,
        pending_ids=pending,
        all_fresh=not pending,
        timed_out=timed_out,
    )


def max_edit_date_per_shared_id(raws: list[dict[str, Any]]) -> dict[str, int]:
    """Build ``{sharedId: max editDate}`` from a list of raw entity dicts.

    Pure: no I/O. Used to compute the freshness target per sharedId from the
    latest Mongo raws the harness has seen. Keying by the raw's own ``sharedId``
    (not by the harness's dict key) naturally handles the delete-revert re-create
    mapping: a re-created raw lives under the old id in ``post_revert`` but its
    ``sharedId`` is the *new* id, so it targets the new id (the one ES indexes it
    under). Dead old ids (deleted by the script, present only in ``before``) are
    excluded by only feeding *alive* raws (``after``/``post_revert`` non-``None``
    values) from the caller, so the settle never polls a gone sharedId to a
    timeout.
    """
    targets: dict[str, int] = {}
    for raw in raws:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("sharedId")
        edit_date = _to_int(raw.get("editDate"))
        if sid and edit_date is not None:
            targets[sid] = max(targets.get(sid, edit_date), edit_date)
    return targets


def entity_unchanged(before_raw: dict[str, Any], after_raw: dict[str, Any]) -> bool:
    """True if the script did not modify the entity (so the revert can be skipped).

    Compares excluding :data:`PLATFORM_MANAGED_FIELDS` (``editDate`` is bumped on
    every save, so a no-op ``save_raw`` would falsely look like a change). Skipping
    the no-op revert avoids an unnecessary ES re-index that would race the cleanup
    delete (the version-conflict orphan root cause, Option A part A). ``after_raw``
    is the post-script Mongo raw; ``None`` (script deleted it) is handled by the
    caller (re-create path), not here.
    """
    a = {k: v for k, v in before_raw.items() if k not in PLATFORM_MANAGED_FIELDS}
    b = {k: v for k, v in after_raw.items() if k not in PLATFORM_MANAGED_FIELDS}
    return a == b


def format_freshness_warning(stage: str, result: FreshnessResult) -> str:
    """Render a timed-out freshness settle as an operator-facing warning string.

    Pure: no I/O. ``stage`` is a short label (``"create"`` / ``"cleanup"``). The
    caller only invokes this when ``result.timed_out`` and ``result.pending_ids``
    are truthy; the text names the pending sharedIds so the operator can
    reconcile the shared ES index (e.g. a targeted reindex) if needed.
    """
    pending = ", ".join(result.pending_ids)
    return (
        f"ES settle timed out at {stage}: {len(result.pending_ids)} of "
        f"{len(result.expected_ids)} dummy sharedId(s) not fresh within the "
        f"deadline - the shared ES index may be inconsistent and need a reindex. "
        f"Pending: {pending}"
    )
