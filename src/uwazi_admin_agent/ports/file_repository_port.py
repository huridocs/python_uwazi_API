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

    @abstractmethod
    async def delete_file(self, file_id: str) -> bool:
        """Delete ONE file row by its ``_id`` (``DELETE /api/files?_id=...``).

        Returns ``True`` on success, ``False`` on a Uwazi error (e.g. the file is
        already gone) or a network failure — best-effort, like the uploads.

        SERVER-SIDE SIDE EFFECTS (Uwazi's FileDelete use case → FilesService.delete):
        the file row is removed, its stored bytes are torn down (async job), and
        every relationship connection citing the file is torn down too
        (``relV1DS.deleteByFiles``), as are V2 text references to it. Callers that
        only delete a byte-identical DUPLICATE (a second copy of content that
        stays on the same entity) lose no content — but any connection citing
        the deleted copy specifically is destroyed with it, so callers must
        keep connection-cited copies (see ``domain/file_cleanup.py``).

        NOT REVERTABLE by the agent's backup/revert machinery: file rows are not
        entity-row writes, so ``BackupIntercept`` cannot snapshot or restore
        them. Uwazi's own activity log (global ``activitylogMiddleware``) still
        records each delete for audit.
        """
        ...
