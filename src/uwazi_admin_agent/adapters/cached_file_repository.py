"""Read-through persistent cache over :class:`FileRepositoryPort` (file bytes).

Uwazi storage filenames are minted fresh on every upload
(``${Date.now()}${random}.${ext}`` — ``app/api/files/filesystem.ts``) and
never rewritten, so ``filename → bytes`` is immutable and a cache hit is
always the true content.

``None`` is NEVER cached: the port maps every non-200 (including transient
5xx blips) to ``None``, so a single failure must not poison the key forever —
misses always re-try the instance.

Uploads delegate untouched: the server mints the new storage filename, so
there is nothing to write through until a later raw fetch reveals the name
(the owning entity's cached raw is dropped by the REVERT use case, which
knows the re-upload target).

Deletes delegate WITHOUT byte eviction here, on purpose: this port only
sees ``file_id`` (not the storage filename the byte cache is keyed by), so
the DELETE HELPER owns eviction — the intercept's ``_record_deleted_files`
evicts the successfully deleted files' cached bytes via
``CacheInvalidationPort.invalidate_files``. A file deleted by someone else
(a human in the Uwazi UI) can still serve cached bytes afterwards —
data-correct (bytes are immutable per filename), and bounded by the
entity-raw TTL once the JOIN view refreshes.
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

    @override
    async def delete_file(self, file_id: str) -> bool:
        # No byte eviction here — see the module docstring: this port only sees
        # file_id, not the storage filename the byte cache is keyed by. The
        # DELETE HELPER owns eviction (the intercept's _record_deleted_files
        # evicts the successfully deleted files' bytes via the invalidation
        # port), which is what keeps our own deletes ghost-free.
        return await self._inner.delete_file(file_id)
