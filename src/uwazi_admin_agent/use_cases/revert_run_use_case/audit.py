# mypy: disable-error-code="attr-defined"
"""Revert-run module split out of ``revert_run_use_case`` (behavior preserved)."""

from __future__ import annotations

from uwazi_admin_agent.domain.audit_record import AuditOutcome, AuditStep, make_audit_record


class AuditMixin:
    """Revert-run audit methods (moved verbatim from the original class)."""

    def _emit_file(self, run_id: str, shared_id: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one file-restore audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="restore_file_failed" if failed else "restore_file",
                shared_ids=[shared_id],
                outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
                detail=detail,
            ),
        )

    def _emit_rel(self, run_id: str, *, failed: bool, detail: str | None = None) -> None:
        """Append one mutual-relationship-restore audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="restore_relationship_failed" if failed else "restore_relationships",
                shared_ids=[],
                outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
                detail=detail,
            ),
        )

    def _emit(self, run_id: str, op_kind: str, shared_ids: list[str]) -> None:
        """Append one revert-action audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind=op_kind,
                shared_ids=shared_ids,
                outcome=AuditOutcome.SUCCESS,
            ),
        )

    def _emit_run(self, outcome: AuditOutcome, run_id: str) -> None:
        """Append a run-level ``revert`` audit record (no-op if no audit log)."""
        if self._audit_log is None:
            return
        self._audit_log.append(
            run_id,
            make_audit_record(
                run_id=run_id,
                step=AuditStep.REVERT,
                op_kind="revert",
                shared_ids=[],
                outcome=outcome,
            ),
        )
