"""``uwazi_api``-backed file-byte access for delete-revert file capture + re-upload.

Delegates to :class:`uwazi_api.use_cases.repositories.file_repository.FileRepository`
over the same :class:`UwaziClient.http` the entity repository uses — raw bytes in,
raw bytes out (§2.5). Does **not** modify ``uwazi_api``; imports and delegates.

Async by signature (the port is async); the underlying ``requests`` calls are
synchronous, matching :class:`UwaziEntityRepository`. Uploads target a
``shared_id`` + ``language`` (the ``locale`` cookie); a missing language defaults
to ``"en"`` so the cookie is always a real locale.
"""

from __future__ import annotations

from typing import override

from loguru import logger

from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_api.client import UwaziClient
from uwazi_api.use_cases.repositories.file_repository import FileRepository

_DEFAULT_LANGUAGE: str = "en"


class UwaziFileRepository(FileRepositoryPort):
    """Raw file-byte access over a :class:`UwaziClient` (§2.5)."""

    def __init__(self, client: UwaziClient) -> None:
        self._repo: FileRepository = FileRepository(client.http)

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self._repo.get_document_by_file_name(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        # Uwazi's document upload endpoint is PDF-centric and
        # upload_document_from_bytes defaults to application/pdf; documents are
        # always PDFs in Uwazi, so the content_type argument is informational
        # here (kept on the port signature for symmetry with attachments).
        del content_type
        ok = self._repo.upload_document_from_bytes(data, shared_id, language or _DEFAULT_LANGUAGE, title)
        if not ok:
            logger.warning("document upload failed sharedId={} title={}", shared_id, title)
        return ok

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        ok = self._repo.upload_file_from_bytes(data, shared_id, language or _DEFAULT_LANGUAGE, title, content_type)
        if not ok:
            logger.warning("attachment upload failed sharedId={} title={}", shared_id, title)
        return ok

    @override
    async def delete_file(self, file_id: str) -> bool:
        # Delegates to uwazi_api's existing DELETE /api/files?_id=... call. The
        # server tears the file row + bytes AND the connections citing the file
        # (see the port's docstring); the caller decides what is safe to delete.
        ok = self._repo.delete_file(file_id)
        if not ok:
            logger.warning("file delete failed file_id={}", file_id)
        return ok
