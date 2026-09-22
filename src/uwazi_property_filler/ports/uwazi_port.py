from abc import ABC, abstractmethod

from uwazi_property_filler.domain.pdf_item import PdfItem


class UwaziPort(ABC):
    """Uwazi I/O boundary (entities, metadata, PDF bytes, entity search)."""

    @abstractmethod
    async def login(self, user: str, password: str) -> None:
        """Validate credentials against the Uwazi instance; raise on failure."""

    @abstractmethod
    async def list_documents(self, template_name: str, language: str, filter_value: str | None = None) -> list[PdfItem]:
        """Entities of ``template_name`` with a primary document in ``language``.

        ``filter_value`` optionally restricts to a single thesaurus label of
        the configured ``FILTER_PROPERTY``; ``None`` returns all entities.
        """

    @abstractmethod
    async def get_entity_metadata(self, shared_id: str, template_name: str, language: str) -> dict:
        """LLM/agent-shaped metadata (labels, not UUIDs; relationship as ``[{"shared_id","title"}]``)."""

    @abstractmethod
    async def update_metadata(self, shared_id: str, template_name: str, language: str, metadata: dict) -> None:
        """Coerce + write metadata via a partial update."""

    @abstractmethod
    async def get_pdf_bytes(self, filename: str) -> bytes | None:
        """Fetch the raw PDF bytes for a storage filename."""

    @abstractmethod
    async def search_entities(self, template_name: str, language: str, query: str | None) -> list[dict[str, str]]:
        """``[{"shared_id","title"}]`` for the relationship option selector."""
