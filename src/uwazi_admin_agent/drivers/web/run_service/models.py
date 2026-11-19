"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.manifest import AppliedWrite, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.plan_intent import PlanOutcome
from uwazi_admin_agent.domain.review_refresh import (
    ReconciliationSummary,
)
from uwazi_admin_agent.domain.snapshot import EntityIdentity


class GenerateError(Exception):
    """Generation failed; the manifest is persisted as GENERATION_FAILED."""


class RevertVerificationError(Exception):
    """Revert succeeded but post-revert verification found mismatches."""


@dataclass(frozen=True)
class RunSummary:
    """One row in the runs table — the manifest's headline fields."""

    run_id: str
    last_executed_at: datetime | None
    status: RunStatus
    created_at: datetime
    prompt: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None


@dataclass(frozen=True)
class RunDetail:
    run_id: str
    status: RunStatus
    created_at: datetime
    prompt: str
    validated_prompt: str | None
    script: str
    modified: int
    deleted: int
    created: int
    rewired: int
    error: str | None
    # What the generation turn declared the prompt asks for, and its own
    # one-line intent (where a weak extractor must say so). Both are shown in the
    # script dialog: without them a refused/limited plan reads as a silent one.
    # ``None`` = generated before the declaration existed (legacy/seeded run).
    plan_kind: str | None = None
    plan_description: str | None = None


@dataclass(frozen=True)
class ExecutionEvent:
    """One row in a run's execution-history modal (derived from audit records)."""

    timestamp: datetime
    type: str  # "execute" | "revert"
    outcome: str  # "success" | "failure"
    detail: str | None


@dataclass(frozen=True)
class TableUpdateResult:
    """What one "Sync with Uwazi" pass changed."""

    run_id: str
    changed_in_uwazi: int | None
    reconciliation: ReconciliationSummary
    # What the plan amounted to AFTER hydration. A pass that recorded hundreds of
    # correct writes whose values Uwazi already holds produces no change rows;
    # reporting only the row count ("1 inserted") reads as a failure, so the
    # message says the entities are up to date.
    outcome: PlanOutcome | None = None

    def message(self) -> str:
        if self.changed_in_uwazi is None:
            changed = "table re-read (Uwazi change scan unavailable)"
        elif self.changed_in_uwazi:
            changed = f"{self.changed_in_uwazi} entity change(s) detected in Uwazi"
        else:
            changed = "no Uwazi changes detected"
        if (
            self.outcome is not None
            and self.outcome.only_dropped_no_ops
            and not self.outcome.matched_rows
            and not self.outcome.skipped_rows
        ):
            return (
                f"Up to date: {changed}; all {self.outcome.unchanged_rows} proposed change(s) already "
                "match Uwazi, so there is nothing to run. "
                f"{self.reconciliation.summary_text()}."
            )
        return f"Synced with Uwazi: {changed}; {self.reconciliation.summary_text()}."


@dataclass(frozen=True)
class RunResults:
    """The touch-set a run produced: which entities/files it changed.

    Unlike :class:`RunDetail` (which reports counts for the table), this carries
    the full identity lists from the manifest so the UI can show exactly which
    entities were modified/created/deleted, which relationships were rewired,
    and which files were deleted. It also carries the script's ``result`` value
    (the answer for a query task, or the summary for a mutation task), the
    ``applied_writes`` (what each write did, with before-values — the diff
    source), and ``titles`` (shared_id → title, resolved without any HTTP call).
    """

    run_id: str
    status: RunStatus
    result: str | None
    modified: list[EntityIdentity]
    created: list[EntityIdentity]
    deleted: list[EntityIdentity]
    rewired: list[RewiredRelationship]
    deleted_files: list[DeletedFile]
    applied_writes: list[AppliedWrite]
    titles: dict[str, str]


@dataclass(frozen=True)
class EntityDocument:
    """An entity's PRIMARY document and the language it was found in.

    ``language`` is the language whose entity row actually carries the file,
    which is not necessarily the one that was asked for: an entity usually
    holds its document in the language it was uploaded in.
    """

    filename: str
    language: str


class EntityBackupInfo(BaseModel):
    """A run's backup record for one entity (the revert button's tooltip source)."""

    model_config = {"frozen": True}

    shared_id: str = Field(description="The entity the backup covers.")
    backup_created_at: datetime | None = Field(
        default=None,
        description="When the entity's single first-touch backup was captured (None = not backed up).",
    )
    languages: list[str] = Field(
        default_factory=list,
        description="The language rows the backup captured (the revert restores each of them).",
    )
    affected_languages: list[str] = Field(
        default_factory=list,
        description="The languages the run was writing when the backup was taken.",
    )


class BulkRevertOutcome(BaseModel):
    """Result of reverting the checked rows' entities (the toast's summary)."""

    model_config = {"frozen": True}

    run_id: str = Field(description="The run the selected rows belong to.")
    reverted: list[str] = Field(
        default_factory=list,
        description="The sharedIds restored to their backup, in the order they were processed.",
    )
    skipped_created: list[str] = Field(
        default_factory=list,
        description="Selected entities the run itself created — their undo is a delete (run-level Revert), refused here.",
    )
    skipped_deleted: list[str] = Field(
        default_factory=list,
        description=(
            "Selected entities the run DELETED — their undo is a re-create, which only the ordered "
            "run-level revert can do (fresh sharedId + files + connection rows), refused here."
        ),
    )
    skipped_no_backup: list[str] = Field(
        default_factory=list,
        description="Selected entities with no backup — the run never wrote to them.",
    )
    failures: list[str] = Field(
        default_factory=list,
        description="Entities whose restore raised (restores that DID land stay reverted).",
    )

    def summary(self) -> str:
        """The operator-facing one-liner (pure)."""
        parts = [f"{len(self.reverted)} reverted"]
        if self.skipped_created:
            parts.append(f"{len(self.skipped_created)} created-by-run (revert at run level)")
        if self.skipped_deleted:
            parts.append(f"{len(self.skipped_deleted)} deleted-by-run (revert at run level)")
        if self.skipped_no_backup:
            parts.append(f"{len(self.skipped_no_backup)} without backup")
        if self.failures:
            parts.append(f"{len(self.failures)} failed")
        return ", ".join(parts)
