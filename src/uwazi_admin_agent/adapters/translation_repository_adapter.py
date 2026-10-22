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

from uwazi_admin_agent.domain.text_cache import translation_cache_key
from uwazi_admin_agent.ports.text_cache_port import TextCachePort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_api.client import UwaziClient


class UwaziTranslationRepository(TranslationPort):
    """Text translation over a :class:`UwaziClient` (§ translations)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def translate(self, text: str, language_from: str, language_to: str) -> str:
        return self._client.translations.translate(text, language_from, language_to).text


class CachedTranslationRepository(TranslationPort):
    """A translation port that serves and fills the persistent text cache.

    Translation is a network round-trip whose answer is a pure function of the
    text and language pair, so the same paragraph translated during a
    prediction pass, an execute pass, or a later table refresh is served from
    disk. Cache hits never reach the network.
    """

    def __init__(self, inner: TranslationPort, cache: TextCachePort) -> None:
        self._inner: TranslationPort = inner
        self._cache: TextCachePort = cache

    @override
    async def translate(self, text: str, language_from: str, language_to: str) -> str:
        key = translation_cache_key(text=text, language_from=language_from, language_to=language_to)
        cached = self._cache.get_text(key)
        if cached is not None:
            return cached
        translated = await self._inner.translate(text, language_from, language_to)
        self._cache.put_text(key, translated)
        return translated
