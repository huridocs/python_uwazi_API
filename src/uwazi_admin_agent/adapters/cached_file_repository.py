"""Read-through persistent cache over :class:`FileRepositoryPort` (file bytes).

Uwazi storage filenames are minted fresh on every upload
(``${Date.now()}${random}.${ext}`` — ``app/api/files/filesystem.ts``) and
never rewritten, so ``filename → bytes`` is immutable and a cache hit is always
the true content. The only divergence from a live GET is a file the server has
since DELETED (Uwazi tears the bytes down with the owning entity): the cache
then still serves the bytes it captured — data-correct, and the delete-path
backup captured the same bytes anyway.

``None`` is NEVER cached: the port maps every non-200 (including transient
5xx blips) to ``None``, so a single failure must not poison the key forever —
misses always re-try the instance.

Uploads delegate untouched: the server mints the new storage filename, so
there is nothing to write through until a later raw fetch reveals the name.
"""

from __future__ import annotations

import time
from typing import override

from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort


class CachedFileRepository(FileRepositoryPort):
    """Decorator adding persistent file-byte caching to any FileRepositoryPort."""

    def __init__(self, inner: FileRepositoryPort, cache: FileCacheStore) -> None:
        self._inner: FileRepositoryPort = inner
        self._cache: FileCacheStore = cache

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        cached = self._cache.get_file_bytes(filename)
        if cached is not None:
            return cached
        started = time.monotonic()
        try:
            data = await self._inner.get_file_bytes(filename)
        finally:
            self._cache.note_file_fetch(time.monotonic() - started)
        if data is not None:
            self._cache.put_file_bytes(filename, data)
        return data

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        return await self._inner.upload_document(data, shared_id, language, title, content_type)

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        return await self._inner.upload_attachment(data, shared_id, language, title, content_type)
