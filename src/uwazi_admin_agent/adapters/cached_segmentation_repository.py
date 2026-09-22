"""Read-through persistent cache over :class:`SegmentationRepositoryPort`.

A document's segmentation ``status`` transitions ``processing → ready`` and,
once ready, never rewrites (the structured paragraphs are re-derived from the
same immutable document bytes), so a READY segmentation is immutable per
``file_id`` — a cache hit is always the true content, exactly like file bytes.

Only ``status == "ready"`` entries are cached, on purpose: a ``processing``
(or otherwise non-ready) result, and every 404/``SegmentationNotFoundError``
(which the port raises rather than returning ``None``), must NOT poison the
key forever — they re-try until the segmentation flips to ``ready``. A
transient failure therefore also stores nothing (the exception propagates
before the ``put``, mirroring ``CachedEntityRepository``'s "a failed read
stores nothing").

Keys are the document ``file_id`` (the ``_id`` ``peek_entity_files`` returns
for kind=document entries) — NOT the storage filename. Deletes delegate
WITHOUT eviction here, mirroring :class:`CachedFileRepository`: the DELETE
HELPER owns eviction via ``CacheInvalidationPort.invalidate_segmentations``
(the intercept's ``_record_deleted_files`` carries every deleted file's
``file_id``), which is lossless by construction — the bytes were persisted to
the run's backup store before the delete.
"""

from __future__ import annotations

import time
from typing import override

from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_api.domain.segmentation import Segmentation


class CachedSegmentationRepository(SegmentationRepositoryPort):
    """Decorator adding persistent ready-segmentation caching to any SegmentationRepositoryPort."""

    def __init__(self, inner: SegmentationRepositoryPort, cache: FileCacheStore) -> None:
        self._inner: SegmentationRepositoryPort = inner
        self._cache: FileCacheStore = cache

    @override
    async def get_by_file_id(self, file_id: str) -> Segmentation:
        cached = self._cache.get_segmentation(file_id)
        if cached is not None:
            return cached
        started = time.monotonic()
        try:
            seg = await self._inner.get_by_file_id(file_id)
        finally:
            self._cache.note_segmentation_fetch(time.monotonic() - started)
        # Only READY segmentations are immutable; a processing/missing result
        # must re-try on the next read instead of being pinned.
        if seg.status == "ready":
            self._cache.put_segmentation(file_id, seg)
        return seg

    @override
    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        # Delegates uncached: the shared_id -> file_id resolution happens inside
        # ``uwazi_api`` (a runtime JOIN over the files collection) and can change
        # on re-upload/human edit, so the shared_id path is a thin passthrough.
        # Caching is keyed by file_id, so only the get_by_file_id seam caches.
        return await self._inner.get_by_shared_id(shared_id, language)
