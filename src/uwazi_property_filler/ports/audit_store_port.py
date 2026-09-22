from abc import ABC, abstractmethod

from uwazi_property_filler.domain.fill_audit import FillAuditRecord


class AuditStorePort(ABC):
    # Named save_audit/list_audit (not save/list_for) because the concrete
    # PostgresStore implements this port alongside SuggestionStorePort, whose
    # save/list_for signatures differ.
    @abstractmethod
    async def save_audit(self, record: FillAuditRecord) -> None: ...

    @abstractmethod
    async def list_audit(self, instance_key: str, shared_id: str) -> list[FillAuditRecord]: ...
