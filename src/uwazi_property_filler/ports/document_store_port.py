from abc import ABC, abstractmethod

from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.pdf_item import PdfItem


class DocumentStorePort(ABC):
    @abstractmethod
    async def upsert_status(self, instance_key: str, item: PdfItem) -> None: ...

    @abstractmethod
    async def list_by_status(self, instance_key: str, status: FillStatus, language: str) -> list[PdfItem]: ...

    @abstractmethod
    async def get_status(self, instance_key: str, shared_id: str, language: str) -> PdfItem | None: ...

    @abstractmethod
    async def mark_validated(self, instance_key: str, shared_id: str, language: str, filled_metadata: dict) -> None: ...
