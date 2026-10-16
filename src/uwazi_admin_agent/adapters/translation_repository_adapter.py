"""``uwazi_api``-backed text translation (§ translations).

Delegates to :class:`uwazi_api.use_cases.repositories.translation_repository.TranslationRepository`
over the same :class:`UwaziClient` the entity/file repositories use — the same
``POST /api/translationService`` proxy Uwazi's own AI-translation flow calls.
Does **not** modify ``uwazi_api``; imports and delegates.

Async by signature (the port is async); the underlying ``requests`` calls are
synchronous, matching :class:`UwaziSegmentationRepository`.
"""

from __future__ import annotations

from typing import override

from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_api.client import UwaziClient


class UwaziTranslationRepository(TranslationPort):
    """Text translation over a :class:`UwaziClient` (§ translations)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def translate(self, text: str, language_from: str, language_to: str) -> str:
        return self._client.translations.translate(text, language_from, language_to).text
