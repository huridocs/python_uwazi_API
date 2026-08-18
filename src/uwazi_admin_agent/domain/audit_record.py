"""The audit-record shape (§5 Phase 6).

One immutable :class:`AuditRecord` per write the agent performs under a run dir.
The audit log is a *record* (append-only, for forensics), not a control-flow
input, so ``op_kind`` is a free string — new ops (Phase 8 capabilities) don't
require a domain edit. ``step`` and ``outcome`` are enum-typed because those are
the structured query keys an operator filters on.

Pure: construction + round-trip via pydantic with literal inputs is the
unit-test target named by the Phase 6 DoD ("audit-record shape"). The
``make_audit_record`` factory stamps ``now(timezone.utc)``; tests construct
:class:`AuditRecord` directly with literal timestamps.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class AuditStep(str, Enum):
    """Which run-lifecycle step emitted the record."""

    EXECUTE = "execute"
    REVERT = "revert"


class AuditOutcome(str, Enum):
    """Whether the op succeeded or failed."""

    SUCCESS = "success"
    FAILURE = "failure"


class AuditRecord(BaseModel):
    """One write performed under a run dir, for the audit log (§5 Phase 6)."""

    model_config = ConfigDict(frozen=True)

    timestamp: datetime = Field(description="When the op was performed (UTC).")
    run_id: str = Field(description="The run this record belongs to.")
    step: AuditStep = Field(description="Which run-lifecycle step emitted the record.")
    op_kind: str = Field(
        description="The op that performed the write — a free string "
        "(e.g. 'update', 'delete', 'create', 'create_relationships', "
        "'restore_entity', 'restore_relationship', 'delete_created', "
        "'cap_exceeded', 'execute', 'revert')."
    )
    shared_ids: list[str] = Field(
        default_factory=list, description="The sharedIds the op touched (empty for run-level records)."
    )
    outcome: AuditOutcome = Field(description="Whether the op succeeded or failed.")
    detail: str | None = Field(default=None, description="Free-text context (error message, cap count, etc.).")


def make_audit_record(
    run_id: str,
    step: AuditStep,
    op_kind: str,
    shared_ids: list[str],
    outcome: AuditOutcome,
    detail: str | None = None,
) -> AuditRecord:
    """Build an :class:`AuditRecord` stamped with the current UTC time."""
    return AuditRecord(
        timestamp=datetime.now(timezone.utc),
        run_id=run_id,
        step=step,
        op_kind=op_kind,
        shared_ids=list(shared_ids),
        outcome=outcome,
        detail=detail,
    )
