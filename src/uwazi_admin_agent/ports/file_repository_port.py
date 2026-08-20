from abc import ABC, abstractmethod


class FileRepositoryPort(ABC):
    """Raw file-byte access for delete-revert file capture + re-upload (§2.5).

    A thin seam over ``uwazi_api``'s :class:`FileRepository` so the backup
    intercept (capture bytes before a delete) and the revert use case (re-upload
    bytes to a re-created entity) stay testable and layer-clean. Implementations
    fetch and upload **raw file bytes** — never validated models — and target a
    ``shared_id`` + ``language`` (the upload's ``locale`` cookie).

    Async by signature (the port is async, matching :class:`EntityRepositoryPort`);
    the underlying ``requests`` calls are synchronous. No ``uwazi_api`` change.
    """

    @abstractmethod
    async def get_file_bytes(self, filename: str) -> bytes | None:
        """Fetch one file's bytes by its storage filename (``GET /api/files/{filename}``).

        Returns ``None`` if the file is absent (e.g. already torn down). Works for
        both documents and uploaded attachments (the route serves both types).
        """
        ...

    @abstractmethod
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        """Upload a primary document/PDF to ``shared_id`` (``POST /api/files/upload/document``).

        Returns ``True`` on success, ``False`` on a Uwazi error/network failure
        (best-effort: the caller logs + records the gap but does not fail the revert).
        """
        ...

    @abstractmethod
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        """Upload an attachment to ``shared_id`` (``POST /api/files/upload/attachment``).

        Returns ``True`` on success, ``False`` on a Uwazi error/network failure.
        """
        ...
