from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.audit_record import AuditRecord


class AuditLogPort(ABC):
    """Append-only audit log of every write performed under a run dir (§5 Phase 6).

    One :class:`AuditRecord` per write (intercepted CRUD op, revert action,
    cap-exceeded, run-level outcome). Pure persistence — no network. Sync:
    filesystem/append ops are fast; the intercept and revert use case call
    these directly without ``await`` (they already run inside the worker thread
    or the async use case's own loop). Records are keyed by ``run_id`` so the
    audit log lives alongside the manifest + snapshots under the run folder.
    """

    @abstractmethod
    def append(self, run_id: str, record: AuditRecord) -> None:
        """Append one audit record to the run's audit log."""
        ...

    @abstractmethod
    def load(self, run_id: str) -> list[AuditRecord]:
        """Load all audit records for a run in append order (empty if absent)."""
        ...
