"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from uwazi_admin_agent.adapters.segmentation_repository_adapter import language_candidates
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
)
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_api.domain.segmentation import Segmentation

from .models import EntityDocument


async def get_entity_segmentation(url: str, user: str, password: str, shared_id: str, language: str) -> Segmentation:
    """Fetch one entity's primary-document segmentation (the entity window's viewer).

    Resolves the entity's document for ``language`` on the controlled instance and
    returns its segmentation. The repository is the runtime's cache-wrapped
    :class:`SegmentationRepositoryPort`, so a repeat view is served from the
    persistent cache. Raises on any failure — the caller degrades to an error label.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    return await runtime.deps.segmentation_repository.get_by_shared_id(shared_id, language)


def document_language_candidates(requested: str | None, configured: list[str]) -> list[str]:
    """Order languages to try for an entity's document (pure).

    The requested language first, then the instance's configured languages —
    the same fallback the segmentation resolver uses, over the same pure
    helper, so a document and its segmentation are always resolved in the same
    language. With no request, the instance's languages are the whole order.
    """
    return language_candidates(requested or DUMMY_LANGUAGE, configured)


async def _instance_languages(runtime: Any) -> list[str]:
    """The instance's configured language keys (``[]`` when settings are unreadable)."""
    settings = getattr(runtime, "settings_repository", None)
    if settings is None:
        return []
    try:
        languages = await asyncio.to_thread(settings.get_languages)
    except Exception as exc:  # noqa: BLE001 — the fallback order is best-effort
        logger.info("could not read the instance's configured languages: {}", exc)
        return []
    return [str(language.key) for language in languages if getattr(language, "key", None)]


async def resolve_entity_document(
    url: str, user: str, password: str, shared_id: str, language: str | None = None
) -> EntityDocument | None:
    """The entity's primary document (and the language it lives in), or None.

    Resolution order: the requested language, then every language the instance
    is configured for. A row's document is frequently uploaded in a language
    other than the one the row is written in (a translated text is updated in
    ``es`` while the PDF was uploaded in ``en``), so a single-language lookup
    finds nothing and the reviewer sees a blank panel. None means the entity
    has no uploaded primary document in ANY configured language — the caller
    then falls back to the document text instead of showing an empty viewer.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    candidates = document_language_candidates(language, await _instance_languages(runtime))
    for candidate in candidates:
        try:
            raw = await runtime.entity_repository.get_raw_by_shared_id(shared_id, candidate)
        except Exception as exc:  # noqa: BLE001 — no locale row for this language
            logger.info("no entity raw for sharedId={} language={}: {}", shared_id, candidate, exc)
            continue
        documents = [ref for ref in extract_file_refs(raw) if ref.kind == "document"]
        if documents:
            return EntityDocument(documents[0].filename, candidate)
    return None


async def fetch_entity_document_bytes(
    url: str, user: str, password: str, shared_id: str, language: str | None = None
) -> bytes | None:
    """Fetch an entity's PRIMARY document bytes for the review window's viewer.

    Resolves the file the same way :func:`resolve_entity_document` does (so the
    viewer's bytes and the window's viewer-or-text decision can never disagree)
    and returns them through the cached file repository — re-opening the same
    entity is free. ``None`` when the entity has no uploaded primary document.
    """
    runtime = build_runtime(user=user, password=password, url=url)
    document = await resolve_entity_document(url, user, password, shared_id, language)
    if document is None:
        return None
    return await runtime.file_repository.get_file_bytes(document.filename)
