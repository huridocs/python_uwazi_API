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

import asyncio
from typing import override

from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.constants import LANGUAGE_TO_FILE_LANGUAGE
from uwazi_api.domain.exceptions import SegmentationNotFoundError
from uwazi_api.domain.segmentation import Segmentation


def language_candidates(requested: str, configured: list[str]) -> list[str]:
    """Order languages to try when resolving an entity's document.

    The requested language first, then the instance's configured languages
    (an entity often only carries a document in its own language, not in the
    row's display language). Duplicates dropped, order preserved. ``en`` and
    its file form ``eng`` count as the same language, so a configured ``en``
    is never retried after the requested ``en`` already failed.
    """
    seen: set[str] = set()
    candidates: list[str] = []
    for lang in [requested, *configured]:
        canonical = LANGUAGE_TO_FILE_LANGUAGE.get(lang, lang)
        if not canonical and not lang:
            continue
        if canonical in seen or (canonical != lang and lang in seen):
            continue
        seen.add(canonical)
        seen.add(lang)
        candidates.append(lang)
    return candidates


class UwaziSegmentationRepository(SegmentationRepositoryPort):
    """Read-only segmentation access over a :class:`UwaziClient` (§ segmentation)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def get_by_file_id(self, file_id: str) -> Segmentation:
        return self._client.files.get_segmentation_by_file_id(file_id)

    @override
    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        def _configured_file_languages() -> list[str]:
            """File-language codes for the languages configured on the instance.

            ``FileService.get_segmentation`` matches documents by
            ``LANGUAGE_TO_FILE_LANGUAGE`` (``es``→``spa``, …), so each configured
            key is translated; keys outside the mapping are tried verbatim (an
            instance may carry LCID keys like ``deu`` directly).
            """
            return [
                LANGUAGE_TO_FILE_LANGUAGE.get(key, key)
                for key in (lang.key for lang in self._client.settings.get_languages())
            ]

        try:
            return self._client.files.get_segmentation(shared_id, language)
        except SegmentationNotFoundError:
            # The entity may hold its document only in one of the instance's
            # configured languages (an entity often carries a PDF only in the
            # language it was uploaded in). Retry the configured languages
            # instead of failing outright.
            configured = await asyncio.to_thread(_configured_file_languages)
            for candidate in language_candidates(language, configured)[1:]:
                try:
                    return self._client.files.get_segmentation(shared_id, candidate)
                except SegmentationNotFoundError:
                    continue
            raise
