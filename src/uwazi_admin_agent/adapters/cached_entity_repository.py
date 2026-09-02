"""Read-through persistent cache over :class:`EntityRepositoryPort` (entity raws).

Entity raws are mutable — the agent writes them (execute/revert) and humans
edit Uwazi directly — so entries expire via the TTL AND are invalidated the
moment our own code writes an entity: ``save_raw`` / ``delete_by_shared_id``
invalidate right here, and sandbox CRUD writes (which bypass this port via
``EntityApiPort``) are invalidated by :class:`BackupIntercept` through the
same :class:`CacheInvalidationPort`. Direct human edits remain bounded by the
TTL alone — that residual staleness window is the documented Phase-2 trade-off
(fresh snapshots stay server-truth by default; see
``ENTITY_CACHE_FRESH_SNAPSHOTS``).

``get_raw_by_internal_id`` delegates uncached (rarely used; different key) and
``create_raw`` has nothing to invalidate (a created entity mints a fresh
sharedId nothing is cached under).
"""

from __future__ import annotations

import time
from typing import Any, override

from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort


class CachedEntityRepository(EntityRepositoryPort):
    """Decorator adding TTL'd raw-entity caching + write-path invalidation."""

    def __init__(self, inner: EntityRepositoryPort, cache: FileCacheStore) -> None:
        self._inner: EntityRepositoryPort = inner
        self._cache: FileCacheStore = cache

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        cached = self._cache.get_raw(shared_id, language)
        if cached is not None:
            return cached
        started = time.monotonic()
        try:
            raw = await self._inner.get_raw_by_shared_id(shared_id, language)
        finally:
            self._cache.note_raw_fetch(time.monotonic() - started)
        # Never reached when the inner fetch raised — a failed read stores nothing.
        self._cache.put_raw(shared_id, language, raw)
        return raw

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        return await self._inner.get_raw_by_internal_id(internal_id)

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        await self._inner.save_raw(raw)
        shared_id = raw.get("sharedId")
        if isinstance(shared_id, str) and shared_id:
            self._cache.invalidate_entities([shared_id])

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        return await self._inner.create_raw(raw)

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        await self._inner.delete_by_shared_id(shared_id)
        self._cache.invalidate_entities([shared_id])
