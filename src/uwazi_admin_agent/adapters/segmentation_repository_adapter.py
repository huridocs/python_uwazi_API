"""``uwazi_api``-backed document segmentation access (§ segmentation).

Delegates to :class:`uwazi_api.use_cases.file_service.FileService` over the same
:class:`UwaziClient` the entity/file repositories use — validated
:class:`Segmentation` models in, validated models out. Does **not** modify
``uwazi_api``; imports and delegates (``client.files`` already exposes
``get_segmentation_by_file_id`` / ``get_segmentation``).

Async by signature (the port is async); the underlying ``requests`` calls are
synchronous, matching :class:`UwaziEntityRepository`.
"""

from __future__ import annotations

from typing import override

from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.segmentation import Segmentation


class UwaziSegmentationRepository(SegmentationRepositoryPort):
    """Read-only segmentation access over a :class:`UwaziClient` (§ segmentation)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def get_by_file_id(self, file_id: str) -> Segmentation:
        return self._client.files.get_segmentation_by_file_id(file_id)

    @override
    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        return self._client.files.get_segmentation(shared_id, language)
