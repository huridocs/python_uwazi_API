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


def strict_settle_targets(
    after: dict[str, dict[str, Any] | None],
    post_revert: dict[str, dict[str, Any] | None],
    reverted: set[str],
) -> dict[str, int]:
    """Build the strict settle targets for the cleanup delete.

    Pure: no I/O. For each alive dummy, returns the ``editDate`` the ES doc must
    be ``>=`` to guarantee the ``deleteByQuery`` snapshots the LATEST version.

    A **reverted** dummy (the script modified it and the revert re-indexed it) is
    the orphan risk: its revert ``save_raw`` bumps ``editDate``, but ``editDate``
    is a millisecond timestamp that can collide with the pre-revert value. A
    plain ``>= latest`` check would then pass against the stale pre-revert version
    and the delete would skip the unrefreshed revert re-index. So a reverted
    dummy's target is its **pre-revert** ``editDate + 1`` (``>=`` becomes
    "strictly greater than the pre-revert editDate"), which only the revert's
    re-index can satisfy. Every other dummy (unchanged, script-created,
    re-created, or modified-but-not-reverted on a script error) has no such
    collision window and keeps the plain ``>= latest`` target.
    """
    targets: dict[str, int] = {}
    for sid, raw in after.items():
        if raw is None:
            continue
        edit_date = _to_int(raw.get("editDate"))
        if edit_date is None:
            continue
        targets[sid] = edit_date + 1 if sid in reverted else edit_date
    for raw in post_revert.values():
        if raw is None:
            continue
        actual_sid = raw.get("sharedId")
        edit_date = _to_int(raw.get("editDate"))
        if not actual_sid or edit_date is None or actual_sid in targets:
            continue
        targets[actual_sid] = edit_date
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


def identify_leftover_shared_ids(observed: dict[str, int | None]) -> list[str]:
    """Return the sharedIds still present in ES after a delete (probe returned a non-None editDate).

    Pure: no I/O. After the cleanup delete, a cleanly-deleted dummy's ES doc is
    gone (the probe returns ``None``); an orphan (``deleteByQuery`` skipped it on
    version conflict, then the trailing ``refresh`` flushed it) is still present
    (the probe returns an ``editDate``). This is the post-delete verification
    seam: the harness re-probes each sharedId after ``_delete_all`` and feeds the
    results here to name any leftover sharedIds.
    """
    return [sid for sid, edit_date in observed.items() if edit_date is not None]


def format_orphan_error(leftover: list[str]) -> str:
    """Render a hard, actionable error naming the orphaned sharedIds.

    Pure: no I/O. An orphan (Mongo row gone, ES doc still present) cannot be
    removed via the API — Uwazi's delete path queries Mongo first
    (``MongoEntityPermissionChecker.filterEntities``) and, finding nothing, never
    issues the ES delete. The operator must reconcile the shared ES index (a
    targeted reindex or direct ES access). The message names the leftover
    sharedIds so the operator knows exactly what to reconcile.
    """
    ids = ", ".join(leftover)
    return (
        f"Cleanup left {len(leftover)} orphan entity/entities in ElasticSearch "
        f"(Mongo rows already deleted, so they cannot be removed via the API): "
        f"{ids}. The shared ES index needs a targeted reindex of these sharedIds."
    )


def format_settle_blocked_error(pending: list[str]) -> str:
    """Render a hard error for a cleanup settle that timed out with not-fresh dummies.

    Pure: no I/O. When the cleanup settle times out, deleting the pending dummies
    would risk an unrecoverable ES orphan (``deleteByQuery`` skips the unrefreshed
    revert re-index on version conflict). Instead the harness leaves them in Mongo
    (recoverable via the normal delete path) and surfaces this error naming the
    pending sharedIds so the operator can reconcile them.
    """
    ids = ", ".join(pending)
    return (
        f"Cleanup blocked: {len(pending)} dummy sharedId(s) were not ES-fresh within "
        f"the settle deadline, so they were left in Mongo (recoverable) rather than "
        f"risking an unrecoverable ElasticSearch orphan. Left-behind sharedIds: {ids}. "
        f"Re-run validation or delete these sharedIds manually."
    )
