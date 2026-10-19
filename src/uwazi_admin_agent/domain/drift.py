"""Drift detection between a review run's snapshot and live Uwazi (pure domain).

The review table's rows are a point-in-time projection of Uwazi. Outside edits
(manual or via another tool) and new/deleted entities make that projection
stale. Detection rides Uwazi's server-managed ``editDate`` (bumped on every
save) plus an entity count per template — never full metadata re-reads.

Two levels, cheapest first:

* **Probe** (:class:`DriftProbe`): one ``/api/v2/search`` call asking for the
  10 most recently edited entities' ``sharedId``/``editDate`` (sorted on the
  indexed ``editDate.sort`` sub-field) + one ``count_by_template`` call. Any
  of those ``editDate`` values post-dating the run's baseline — excluding the
  ids the run itself wrote — or a changed count signals drift. Two small
  round-trips, answers "is the table still valid?".
* **Scan** (:class:`DriftReport`): a paged ``/api/v2/search`` over the whole
  template collecting only ``sharedId``/``editDate`` (~tens of bytes per hit),
  whose diff against the baseline classifies each drift into **modified**
  (editDate advanced), **missing** (deleted in Uwazi) and **new** (created in
  Uwazi but absent from the baseline).

Baseline bookkeeping: :class:`DriftBaseline` is the per-(sharedId, language)
``editDate`` snapshot recorded at discover/sync time, and
:func:`build_probe_result` / :func:`build_report` hold ALL the comparison and
label logic — the adapter stores and the UI consume, they never re-decide.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class DriftKind(str, Enum):
    """How one entity's relationship to the run changed since the baseline."""

    MODIFIED = "modified"
    NEW = "new"
    MISSING = "missing"


MODIFIED_LABEL: str = "Modified in Uwazi"
NEW_LABEL: str = "New in Uwazi"
MISSING_LABEL: str = "Deleted in Uwazi"

DRIFT_LABEL: dict[DriftKind, str] = {
    DriftKind.MODIFIED: MODIFIED_LABEL,
    DriftKind.NEW: NEW_LABEL,
    DriftKind.MISSING: MISSING_LABEL,
}


class DriftStatus(str, Enum):
    """The review table's last known drift verdict (the banner's text)."""

    UNKNOWN = "unknown"  # never checked, or invalidated by a new apply
    VALID = "valid"  # last probe found no manual edits and no count change
    DRIFTED = "drifted"  # last probe/scan found changes in Uwazi
    ERROR = "error"  # the check itself failed (network/instance down)


class DriftStateRow(BaseModel):
    """One run's persisted check outcome (the ``drift_state`` table row)."""

    model_config = {"frozen": True}

    run_id: str = Field(description="The run the verdict belongs to.")
    status: DriftStatus = Field(description="The last check's verdict.")
    baseline_max_edit_date: int | None = Field(
        default=None,
        description="The baseline's max editDate at the last check (drift = anything newer).",
    )
    live_count: int | None = Field(
        default=None,
        description="The live entity count at the last check; the baseline's expected count.",
    )
    checked_at: datetime | None = Field(default=None, description="When the last check ran (UTC).")
    checked_by: str | None = Field(
        default=None,
        description="Who/what ran it: 'auto' (page open) or the operator's username for a manual click.",
    )
    detail: str | None = Field(
        default=None,
        description="Short human summary of the last check ('3 modified, 1 new'), or the error text.",
    )


class DriftBaselineRow(BaseModel):
    """One baseline persistence row (the ``drift_baseline`` table row)."""

    model_config = {"frozen": True}

    run_id: str = Field(description="The run the baseline belongs to.")
    shared_id: str = Field(description="The entity's sharedId.")
    language: str | None = Field(default=None, description="The row's language (None = language-independent group).")
    edit_date: int | None = Field(default=None, description="Uwazi editDate at capture time; None = never seen.")
    title: str | None = Field(default=None, description="Entity title at capture time (for new-entity display).")
    captured_at: datetime | None = Field(default=None, description="When this row was recorded (UTC).")


class DriftEntry(BaseModel):
    """One entity whose state diverged from the baseline."""

    model_config = {"frozen": True}

    shared_id: str = Field(description="The drifted entity's sharedId.")
    kind: DriftKind = Field(description="How it diverged (modified / new / missing).")
    edit_date: int | None = Field(
        default=None,
        description="The live editDate (modified/new); None for missing entities.",
    )


class DriftProbe(BaseModel):
    """The cheap two-request check: is the table still valid?

    ``drifted`` is True iff any probed editDate post-dates the baseline (the
    run's own writes excluded via ``excluded_ids``) or the entity count moved.
    ``entities`` is the ids behind any editDate drift, so a positive probe can
    be followed by a full :func:`build_report` scan.
    """

    model_config = {"frozen": True}

    drifted: bool = Field(description="True when manual edits or count changes were detected.")
    entities: list[DriftEntry] = Field(
        default_factory=list,
        description="The probed entities (not the run's own) whose editDate post-dates the baseline.",
    )
    count_drift: bool = Field(
        default=False,
        description="Live count differs from the baseline; a probe alone cannot prove deletions.",
    )
    live_count: int | None = Field(
        default=None,
        description="The template's live entity count (None = the count request failed).",
    )
    checked_at: datetime = Field(description="When the probe ran (UTC).")
    max_live_edit_date: int | None = Field(
        default=None,
        description="The highest editDate the probe saw (the next baseline's floor).",
    )
    languages_failed: list[str] = Field(
        default_factory=list,
        description="Languages whose probe request failed (the verdict could miss drift in them).",
    )


class DriftReport(BaseModel):
    """The full classification of every drifted entity (the paged scan)."""

    model_config = {"frozen": True}

    modified: list[DriftEntry] = Field(
        default_factory=list,
        description="Entities whose live editDate exceeds their baseline value (run-written ids excluded).",
    )
    new: list[DriftEntry] = Field(
        default_factory=list,
        description="Live entities the baseline never recorded (created in Uwazi after the snapshot).",
    )
    missing: list[DriftEntry] = Field(
        default_factory=list,
        description="Baseline entities no longer present in Uwazi (deleted there).",
    )
    checked_at: datetime = Field(description="When the scan ran (UTC).")
    languages_failed: list[str] = Field(
        default_factory=list,
        description="Languages whose scan failed (their ids kept as present, so no false 'deleted').",
    )

    def entries(self) -> list[DriftEntry]:
        """All drifted entities, modified → new → missing (deterministic display order)."""
        return [*self.modified, *self.new, *self.missing]

    def total(self) -> int:
        return len(self.modified) + len(self.new) + len(self.missing)


class DriftBaseline(BaseModel):
    """The per-entity ``editDate`` snapshot the probes compare against.

    One row per (sharedId, language) the run covered. ``missing`` rows carry
    ``language=None``. ``captured_at`` is when the row was synced (set by the
    adapter's discovery/apply path, not by the check itself).
    """

    model_config = {"frozen": True}

    run_id: str = Field(description="The run the baseline belongs to.")
    shared_id: str = Field(description="The entity's sharedId.")
    language: str | None = Field(
        default=None,
        description="The entity row's language (None = the language-independent group).",
    )
    edit_date: int | None = Field(
        default=None,
        description="Uwazi's editDate when this run's data was captured; None means never seen (no drift knowledge).",
    )
    captured_at: datetime | None = Field(default=None, description="When this baseline row was last synced (UTC).")


def max_live_edit_date(probed: dict[str, int | None]) -> int | None:
    """The highest parseable editDate among probed hits (pure).

    ``probed`` maps sharedId → editDate (the adapter's flat shape). Non-numeric
    values are ignored; an empty/failed probe returns None so the caller keeps
    the previous baseline's max rather than resetting.
    """
    dates = [v for v in probed.values() if isinstance(v, int) and not isinstance(v, bool)]
    return max(dates) if dates else None


def build_probe_result(
    probed: dict[str, int | None],
    baseline_max_by_shared_id: dict[str, int | None],
    excluded_ids: set[str],
) -> list[DriftEntry]:
    """Classify the probe's hits against the baseline maximums (pure).

    ``probed`` maps sharedId → editDate (ms epoch; None = unparseable), the
    adapter's shape. ``baseline_max_by_shared_id`` maps each baseline sharedId
    to the max editDate recorded for it (None = unknown — treated as no-drift
    so a run synced before this feature cannot be spam-flagged). An id in
    ``excluded_ids`` (the run's own writes) never counts as drift: its bump is
    the run's own doing.
    """
    entries: list[DriftEntry] = []
    for shared_id, live in probed.items():
        if not isinstance(live, int) or isinstance(live, bool):
            continue
        if shared_id in excluded_ids:
            continue
        base = baseline_max_by_shared_id.get(shared_id)
        if base is None:
            continue
        if live > base:
            entries.append(DriftEntry(shared_id=shared_id, kind=DriftKind.MODIFIED, edit_date=live))
    return entries


def build_report(
    live: list[dict[str, Any]],
    baseline: dict[str, int | None],
    excluded_ids: set[str],
    languages_failed: list[str],
    checked_at: datetime,
) -> DriftReport:
    """The full baseline↔live diff (pure).

    ``live`` is the paged scan's records (``shared_id`` + integer-ish
    ``edit_date``); ``baseline`` maps sharedId → the max recorded editDate
    (None = never seen). Classification:

    * live ``edit_date > baseline`` (and id not run-written) → **modified**
    * live id absent from the baseline → **new**
    * baseline id absent from the live scan → **missing**, unless any scan
      language failed (a partial scan must not flag deletions it never saw).

    A live hit without a parseable editDate cannot prove modification; it
    still counts for presence (its id is not 'missing').
    """
    modified: list[DriftEntry] = []
    new: list[DriftEntry] = []
    live_ids: set[str] = set()
    for record in live:
        shared_id = record["shared_id"]
        live_ids.add(shared_id)
        live_date: int | None = None
        if isinstance(record.get("edit_date"), int) and not isinstance(record.get("edit_date"), bool):
            live_date = record["edit_date"]
        if shared_id not in baseline:
            new.append(DriftEntry(shared_id=shared_id, kind=DriftKind.NEW, edit_date=live_date))
            continue
        base = baseline.get(shared_id)
        if base is not None and live_date is not None and live_date > base and shared_id not in excluded_ids:
            modified.append(DriftEntry(shared_id=shared_id, kind=DriftKind.MODIFIED, edit_date=live_date))
    missing = (
        [DriftEntry(shared_id=sid, kind=DriftKind.MISSING) for sid in baseline if sid not in live_ids]
        if not languages_failed
        else []
    )
    return DriftReport(
        modified=modified,
        new=new,
        missing=missing,
        languages_failed=list(languages_failed),
        checked_at=checked_at,
    )
