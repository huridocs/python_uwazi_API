from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from uwazi_admin_agent.domain.plan import MigrationPlan


class RunStatus(str, Enum):
    """Lifecycle of a migration run."""

    planned = "planned"
    snapshotted = "snapshotted"
    executed = "executed"
    verified = "verified"
    reverted = "reverted"
    failed = "failed"


class EntityIdentity(BaseModel):
    """Identifies one entity row in a manifest

    ``internal_id`` is Uwazi's unique per-row id (used to load snapshots and to
    delete created entities); ``shared_id`` groups language variants and is for
    readability only.
    """

    internal_id: str
    shared_id: str
    language: str | None = None


class RewiredRelationship(BaseModel):
    """A relationship that was repointed during a run, with its before-state.

    Revert restores this relationship to ``before`` (raw, §2.5).
    """

    entity: EntityIdentity
    relationship_type: str
    before: Any = Field(description="Raw before-state of this relationship, as Uwazi returned it.")


class MigrationManifest(BaseModel):
    """The per-run record enabling exact revert (§2.3).

    Revert = restore modified entities + restore rewired relationships + delete
    created entities, in that category order (§2.6).
    """

    run_id: str
    created_at: datetime
    plan: MigrationPlan
    modified: list[EntityIdentity] = Field(
        default_factory=list, description="Existed pre-run, modified -> restore on revert."
    )
    created: list[EntityIdentity] = Field(default_factory=list, description="Created by the run -> delete on revert.")
    rewired: list[RewiredRelationship] = Field(
        default_factory=list, description="Repointed relationships + before-state -> restore on revert."
    )
    status: RunStatus = RunStatus.planned
    snapshots_location: str = Field(description="Where this run's snapshots live (for revert to find them).")
