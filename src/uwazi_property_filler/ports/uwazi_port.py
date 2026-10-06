from abc import ABC, abstractmethod

from uwazi_api.domain.reference import Reference
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
        When filtering, the result set is fetched with one request per value,
        paged under Uwazi's 10 000-result search window, so values larger than
        that window are complete.
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

    @abstractmethod
    async def create_relationship(
        self,
        shared_id: str,
        file_id: str,
        reference: Reference,
        to_entity_shared_id: str,
        relationship_type_id: str,
        language: str,
    ) -> None:
        """Link a text reference on ``shared_id``'s document to a target entity."""

    @abstractmethod
    async def list_relationships(self, shared_id: str, language: str) -> list[dict]:
        """Raw ``relations`` (denormalized connections) for one entity."""

    @abstractmethod
    async def delete_relationships(self, hubs: list[str], language: str) -> None:
        """Delete connection hubs by ObjectId."""

    @abstractmethod
    async def get_file_id(self, shared_id: str, language: str) -> str | None:
        """The Uwazi file id of ``shared_id``'s primary document (or ``None``)."""

    @abstractmethod
    async def relationship_type_id(self, name_or_id: str) -> str | None:
        """Resolve a relationship type name/id to its id (or ``None``)."""
