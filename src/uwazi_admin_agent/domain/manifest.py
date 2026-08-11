from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.plan import MigrationPlan
from uwazi_admin_agent.domain.snapshot import EntityIdentity


class RunStatus(str, Enum):
    """Lifecycle of a migration run (§5.2)."""

    PLANNED = "planned"
    SNAPSHOTTED = "snapshotted"
    EXECUTED = "executed"
    VERIFIED = "verified"
    REVERTED = "reverted"
    FAILED = "failed"


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

    No ``created``/``deleted`` category - no current op creates or deletes
    entities; that lands in Phase 9 if a create-op ever arrives. Kept mutable
    because run status updates over the lifecycle.
    """

    run_id: str = Field(description="Unique run identifier.")
    created_at: datetime = Field(description="When the run was created.")
    plan: MigrationPlan = Field(description="The originating migration plan.")
    modified: list[EntityIdentity] = Field(
        default_factory=list,
        description="Entities that existed pre-run and were modified.",
    )
    rewired: list[RewiredRelationship] = Field(
        default_factory=list,
        description="Rewired relationships with their before-state.",
    )
    status: RunStatus = Field(default=RunStatus.PLANNED, description="Current run status.")
    snapshot_dir: str | None = Field(
        default=None,
        description="Where this run's snapshots live, if persisted (§5.2).",
    )
