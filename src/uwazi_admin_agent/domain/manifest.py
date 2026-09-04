from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.deleted_file import DeletedFile
from uwazi_admin_agent.domain.snapshot import EntityIdentity


class RunStatus(str, Enum):
    """Lifecycle of a migration run (§5.2)."""

    PLANNED = "planned"
    SNAPSHOTTED = "snapshotted"
    EXECUTED = "executed"
    VERIFIED = "verified"
    REVERTED = "reverted"
    FAILED = "failed"
    GENERATION_FAILED = "generation_failed"


class RewiredRelationship(BaseModel):
    """A relationship that was rewired during a run, with its before-state.

    Revert restores the relationship on ``entity`` to ``before``. Recorded per
    §2.3 so revert is exact.
    """

    model_config = ConfigDict(frozen=True)

    entity: EntityIdentity = Field(description="The entity whose relationship was rewired.")
    property_name: str = Field(description="The relationship field/property that changed.")
    before: Any = Field(description="The raw before-state of that relationship field.")


class MigrationManifest(BaseModel):
    """The per-run record enabling exact revert (§2.3).

    Carries the originating ``prompt`` and the generated ``script`` (v2: the
    LLM generates a script, not a declarative plan), plus the entities
    ``modified`` and relationships ``rewired``, plus entities ``created`` and
    ``deleted`` by the script (Phase 4 — scripts can create/delete, unlike v1's
    ops), plus the ``deleted_files`` the run deleted (file rows are not entity
    writes, so they get their own revert section — see :class:`DeletedFile`).
    Kept mutable because run status updates over the lifecycle.
    """

    run_id: str = Field(description="Unique run identifier.")
    created_at: datetime = Field(description="When the run was created.")
    prompt: str = Field(description="The operator's natural-language request that originated the run.")
    script: str = Field(description="The generated Python script that was executed.")
    modified: list[EntityIdentity] = Field(
        default_factory=list,
        description="Entities that existed pre-run and were modified (or relationship-touched).",
    )
    rewired: list[RewiredRelationship] = Field(
        default_factory=list,
        description="Rewired relationships with their before-state.",
    )
    created: list[EntityIdentity] = Field(
        default_factory=list,
        description="Entities created by the script (revert = delete by shared_id).",
    )
    deleted: list[EntityIdentity] = Field(
        default_factory=list,
        description="Entities that existed pre-run and were deleted by the script (revert = restore from snapshot).",
    )
    deleted_files: list[DeletedFile] = Field(
        default_factory=list,
        description=(
            "FILE rows the run deleted (dedupe cleanup or explicit deletion), with the "
            "metadata revert needs to re-upload each one's captured bytes. File deletes "
            "are not entity writes, so they cannot ride a snapshot; this is their own "
            "revert section (populated on the script thread after the deletion batch "
            "joins, successful deletes only)."
        ),
    )
    status: RunStatus = Field(default=RunStatus.PLANNED, description="Current run status.")
    last_executed_at: datetime | None = Field(
        default=None,
        description="When this run was last executed or reverted (UTC); None until the first execution.",
    )
    snapshot_dir: str | None = Field(
        default=None,
        description="Where this run's snapshots live, if persisted (§5.2).",
    )
    error: str | None = Field(
        default=None,
        description="Full error detail from the last failed lifecycle step; cleared on success.",
    )
    error_step: str | None = Field(
        default=None,
        description="Which step failed: 'generate' | 'execute' | 'revert' | 'verify'.",
    )

    def reset_touch_set(self) -> None:
        """Clear the touch-set lists in place (re-execute entry point).

        The manifest carries one execution's touch set, not a cumulative
        history. On re-execute (e.g. after revert) the lists are cleared so the
        intercept repopulates them from scratch; ``prompt``/``script``/
        ``created_at``/``run_id`` are preserved.
        """
        self.modified = []
        self.rewired = []
        self.created = []
        self.deleted = []
        self.deleted_files = []
