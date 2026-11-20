"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import AI_TRANSLATED_MARKER
from uwazi_admin_agent.domain.file_deletion import group_deletions_by_entity
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.source_url import find_source_page_url
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.translation_port import TranslationPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort, UrlFetchError
from uwazi_admin_agent.use_cases.file_transfer import move_files_for_target
from uwazi_admin_agent.use_cases.parallel_script_helpers import (
    assert_unique_deletion_requests,
    assert_unique_move_targets,
    assert_unique_shared_ids,
)
from uwazi_admin_agent.use_cases.segmentation_tools import segmentation_to_dict
from uwazi_api.domain.exceptions import SegmentationNotFoundError

from .scope import assert_ids_in_scope


def _scoped_write_helpers(
    crud: tuple,
    scope: set[str],
) -> dict[str, Any]:
    """Wrap the reused sync CRUD helpers with dummy-scope enforcement.

    ``create_entities`` is allowed (it makes new throwaway entities) and records
    each new shared_id into the live ``scope`` set so cleanup covers them. The
    mutating helpers (``update``/``delete``/publish/``create_relationships``)
    refuse any shared_id outside ``scope``. ``create_relationships`` requires
    both endpoints in scope.
    """
    create_entities = crud[0]
    update_entities = crud[1]
    delete_entities = crud[2]
    publish_entities = crud[3]
    unpublish_entities = crud[4]
    set_publish_status = crud[5]
    create_relationships = crud[6]

    def create_entities_scoped(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        results = create_entities(entities_dicts, language)
        for r in results:
            sid = r.get("shared_id") if isinstance(r, dict) else None
            if sid:
                scope.add(sid)
        return results

    def update_entities_scoped(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        assert_ids_in_scope([e.get("shared_id") for e in entities_dicts if e.get("shared_id")], scope, "update_entities")
        return update_entities(entities_dicts, language)

    def delete_entities_scoped(shared_ids: list[str], language: str | None = None) -> list[dict]:
        del language  # ignored: delete is by sharedId across ALL language rows
        assert_ids_in_scope(shared_ids, scope, "delete_entities")
        return delete_entities(shared_ids)

    def set_publish_status_scoped(shared_ids: list[str], published: bool, language: str | None = None) -> list[dict]:
        del language  # ignored: publish/unpublish act on all language rows by sharedId
        assert_ids_in_scope(shared_ids, scope, "set_publish_status")
        return set_publish_status(shared_ids, published)

    def publish_entities_scoped(shared_ids: list[str], language: str | None = None) -> dict:
        del language
        assert_ids_in_scope(shared_ids, scope, "publish_entities")
        return publish_entities(shared_ids)

    def unpublish_entities_scoped(shared_ids: list[str], language: str | None = None) -> dict:
        del language
        assert_ids_in_scope(shared_ids, scope, "unpublish_entities")
        return unpublish_entities(shared_ids)

    def create_relationships_scoped(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        ids: list[str] = []
        for r in relationships_dicts:
            for key in ("from_entity_shared_id", "to_entity_shared_id"):
                if r.get(key):
                    ids.append(r[key])
        assert_ids_in_scope(ids, scope, "create_relationships")
        return create_relationships(relationships_dicts, language)

    return {
        "create_entities": create_entities_scoped,
        "update_entities": update_entities_scoped,
        "delete_entities": delete_entities_scoped,
        "publish_entities": publish_entities_scoped,
        "unpublish_entities": unpublish_entities_scoped,
        "set_publish_status": set_publish_status_scoped,
        "create_relationships": create_relationships_scoped,
    }


def _move_files_noop_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``move_files_to_entity`` bound into the namespace.

    Dummies are created via ``create_entities`` (no uploaded files), so file-move is
    a no-op in validation: the helper scope-asserts the endpoints (defense-in-depth,
    matching the other scoped write helpers) and returns a ``moved=0`` summary.
    The real file-move logic is exercised only live (file ops cannot be
    gate-validated on dummies - same inherent limitation as delete-revert file
    restore, which is also "live confirmation pending").
    """

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        assert_ids_in_scope([*from_shared_ids, to_shared_id], scope, "move_files_to_entity")
        return {"moved": 0, "failed": 0, "skipped": 0, "note": "no-op in validation - dummies carry no uploaded files"}

    return move_files_to_entity


def _move_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``move_files_to_entity_parallel``.

    Mirrors ``_move_files_noop_scoped`` for the moves-list contract: dummies
    carry no uploaded files, so the parallel move is a no-op in validation.
    It keeps the real helper's two up-front guards so the identical script
    fails identically in every mode: :func:`assert_unique_move_targets`
    (the same-row lost-file race guard) and the scope assertion over every
    move's ``from`` + ``to`` ids (defense-in-depth). Returns one ``moved=0``
    summary per move, in input order — the real helper's result shape.
    """

    def move_files_to_entity_parallel(moves: list[dict], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_move_targets(moves)
        ids: list[str] = [sid for move in moves for sid in [*move["from_shared_ids"], move["to_shared_id"]]]
        assert_ids_in_scope(ids, scope, "move_files_to_entity_parallel")
        return [{"to_shared_id": move["to_shared_id"], "moved": 0, "failed": 0, "skipped": 0} for move in moves]

    return move_files_to_entity_parallel


def _dedupe_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``dedupe_entity_files_parallel``.

    Dummies are created via ``create_entities`` (no uploaded files), so
    duplicate-file cleanup is a no-op in validation: the helper keeps the
    real helper's two up-front guards so the identical script fails
    identically in every mode — :func:`assert_unique_shared_ids` (the
    double-delete race guard) and the scope assertion over every shared_id
    (defense-in-depth) — and returns one zero-count summary per entity, in
    input order: the real helper's result shape. The real cleanup logic is
    exercised only live (file ops cannot be gate-validated on dummies — the
    same documented limitation as the file-move helpers).
    """

    def dedupe_entity_files_parallel(shared_ids: list[str], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_shared_ids(shared_ids, "dedupe_entity_files_parallel")
        assert_ids_in_scope(list(shared_ids), scope, "dedupe_entity_files_parallel")
        return [{"shared_id": sid, "duplicates": 0, "deleted": 0, "failed": 0, "kept_cited": 0} for sid in shared_ids]

    return dedupe_entity_files_parallel


def _delete_files_noop_parallel_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``delete_entity_files_parallel``.

    Dummies carry no uploaded files, so explicit deletion is a no-op in
    validation. The helper keeps the real helper's up-front guards so the
    identical script fails identically in every mode —
    :func:`assert_unique_deletion_requests` (the duplicated-request race
    guard), :func:`group_deletions_by_entity` (which also validates every
    request names a shared_id), and the scope assertion over every entity
    (defense-in-depth) — and returns one zero-count summary per DISTINCT
    entity, in first-appearance order: the real helper's result shape.
    """

    def delete_entity_files_parallel(deletions: list[dict], language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_unique_deletion_requests(deletions)
        grouped = group_deletions_by_entity(deletions)
        assert_ids_in_scope([sid for sid, _ in grouped], scope, "delete_entity_files_parallel")
        return [
            {"shared_id": sid, "requested": len(requests), "deleted": 0, "failed": 0, "refused": 0, "refusals": []}
            for sid, requests in grouped
        ]

    return delete_entity_files_parallel


def _build_move_files_real_helper(
    entity_repository: EntityRepositoryPort | None,
    file_repository: FileRepositoryPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str,
) -> Any:
    """Build the real ``move_files_to_entity`` bound into the real exec namespace.

    The body is the shared :func:`move_files_for_target` flow (the same one
    ``move_files_to_entity_parallel`` runs per target): fetch the target's
    raw and build the dedupe index, then per source — fetch its full raw
    (incl. ``documents``/``attachments``), extract the uploaded-file refs
    (documents + uploaded attachments; URL attachments have no bytes and are
    skipped - moving them is a flagged gap), fetch each file's bytes, SKIP it
    when byte-identical content is already on the target, else re-upload via
    ``upload_document``/``upload_attachment`` (documents first, then
    attachments - ``extract_file_refs`` already returns them in that order).

    Skipping duplicates matters: Uwazi never dedupes uploads (every upload
    mints a fresh file row joined to the entity by ``sharedId``), so without
    it, merging N duplicate entities that share the same files multiplies
    those files N times on the target. A skip is decided by a sha256 match
    only - a same-named but different file still uploads (no-loss bias).

    Best-effort, like delete-revert file restore: a failed fetch/upload is
    counted as ``failed`` but does NOT raise - the merge still deletes the sources
    and the target keeps whatever files were moved. Revert removes the moved
    copies again: they are file-collection rows joined by ``sharedId``, not
    fields on the entity row a raw restore can rewrite, so the revert path
    matches each target row against the sources' backed-up bytes and deletes
    only the copies it can verify (see ``RevertRunUseCase._discard_merge_copies``).
    The sources ARE re-created with their files (the delete-revert path captured
    their bytes before the delete).

    If either port is None (e.g. tests with no file repository wired), returns a
    stub that raises a clear ``RuntimeError`` when the script actually calls it -
    so an unwired script fails loudly instead of silently dropping files.
    """
    if entity_repository is None or file_repository is None:

        def move_files_to_entity_unwired(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
            raise RuntimeError(
                "move_files_to_entity requires a wired entity_repository and "
                "file_repository (got None). Wire FileRepositoryPort into the "
                "runtime/execute use case to enable file-move for merges."
            )

        return move_files_to_entity_unwired

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        lang = language or default_language
        return move_files_for_target(
            entity_repository, file_repository, from_shared_ids, to_shared_id, lang, loop.run_until_complete
        )

    return move_files_to_entity


def _get_entity_files_noop_scoped(scope: set[str]) -> Any:
    """Build the dummy-scoped no-op ``get_entity_files`` bound into the namespace.

    Dummies are created via ``create_entities`` (no uploaded files), so file
    fetch is a no-op in validation: the helper scope-asserts the shared_id
    (defense-in-depth, matching the other scoped helpers) and returns ``[]``.
    The real fetch logic is exercised only live — same documented limitation as
    ``move_files_to_entity`` (dummies carry no uploaded files).
    """

    def get_entity_files(shared_id: str, language: str | None = None) -> list[dict]:
        del language  # ignored: dummies carry no files to localize
        assert_ids_in_scope([shared_id], scope, "get_entity_files")
        return []

    return get_entity_files


def _get_file_bytes_noop() -> Any:
    """Build the dummy no-op ``get_file_bytes`` (dummies carry no files)."""

    def get_file_bytes(filename: str) -> bytes | None:
        return None

    return get_file_bytes


def _dummy_parallel_helpers(scoped: dict[str, Any], scope: set[str]) -> dict[str, Any]:
    """Bind the ``*_parallel`` names in DUMMY mode: shapes identical, no threads.

    Validation runs against a handful of dummies, so the parallel write names
    simply ALIAS the scoped sequential helpers — same scope enforcement, same
    return shapes — and the read names mirror their single-item no-ops as
    dicts, while the file-move name mirrors ``_move_files_noop_scoped`` as a
    per-move list, the dedupe name mirrors
    ``_dedupe_files_noop_parallel_scoped`` as a per-entity list, and the
    explicit-deletion name mirrors ``_delete_files_noop_parallel_scoped``.
    The identical script therefore runs unchanged in the gate; real
    parallelism (and the throttle) is exercised only live, like file-move.
    """
    return {
        "update_entities_parallel": scoped["update_entities"],
        "create_entities_parallel": scoped["create_entities"],
        "create_relationships_parallel": scoped["create_relationships"],
        "get_entity_files_parallel": _dummy_files_parallel(scope),
        "get_file_bytes_parallel": _dummy_bytes_parallel(),
        "get_segmentation_parallel": _dummy_segmentation_parallel(),
        "move_files_to_entity_parallel": _move_files_noop_parallel_scoped(scope),
        "dedupe_entity_files_parallel": _dedupe_files_noop_parallel_scoped(scope),
        "delete_entity_files_parallel": _delete_files_noop_parallel_scoped(scope),
    }


def _dummy_files_parallel(scope: set[str]) -> Any:
    """Dummy ``get_entity_files_parallel``: scope-assert, ``[]`` per in-scope id."""

    def get_entity_files_parallel(shared_ids: list[str], language: str | None = None) -> dict[str, list[dict]]:
        del language  # ignored: dummies carry no files to localize
        assert_ids_in_scope(list(shared_ids), scope, "get_entity_files_parallel")
        return {sid: [] for sid in shared_ids}

    return get_entity_files_parallel


def _dummy_bytes_parallel() -> Any:
    """Dummy ``get_file_bytes_parallel``: ``None`` per filename (no stored bytes)."""

    def get_file_bytes_parallel(filenames: list[str]) -> dict[str, bytes | None]:
        return {name: None for name in filenames}

    return get_file_bytes_parallel


def _dummy_segmentation_parallel() -> Any:
    """Dummy ``get_segmentation_parallel``: ``None`` per id (dummies carry no documents).

    Mirrors the single ``get_segmentation`` no-op (``_get_segmentation_noop``), so a
    generated script that calls the bulk helper still runs unchanged in the dummy
    gate and reads ``None`` per entity — matching what the real helper returns for
    an entity with no primary document / no ready segmentation.
    """

    def get_segmentation_parallel(shared_ids: list[str], language: str | None = None) -> dict[str, dict | None]:
        del language  # ignored: dummies carry no documents to segment
        return {sid: None for sid in shared_ids}

    return get_segmentation_parallel


def _build_get_entity_files_real_helper(
    entity_repository: EntityRepositoryPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str,
) -> Any:
    """Build the real ``get_entity_files`` bound into the real exec namespace.

    Fetches the entity's full raw (incl. ``documents``/``attachments``) and
    returns each uploaded file as a plain dict (``file_id``, ``kind``
    "document"|"attachment", ``filename``, ``originalname``, ``language``,
    ``content_type``). URL attachments are skipped (``extract_file_refs`` has no
    stored bytes for them). Unwired ``entity_repository`` -> a stub that raises
    a clear ``RuntimeError`` when the script calls it (loud, not silent).
    """
    if entity_repository is None:

        def get_entity_files_unwired(shared_id: str, language: str | None = None) -> list[dict]:
            raise RuntimeError(
                "get_entity_files requires a wired entity_repository (got None). "
                "Wire EntityRepositoryPort into the runtime/execute use case to "
                "enable supporting-file extraction."
            )

        return get_entity_files_unwired

    def get_entity_files(shared_id: str, language: str | None = None) -> list[dict]:
        lang = language or default_language
        started = time.monotonic()
        raw = loop.run_until_complete(entity_repository.get_raw_by_shared_id(shared_id, lang))
        refs = [ref.model_dump() for ref in extract_file_refs(raw)]
        # debug, not info: a full pass is tens of thousands of calls — the dry-run/
        # execute boundary logs one aggregate line (incl. cache stats) instead.
        logger.debug("script get_entity_files: {} -> {} file(s) ({:.1f}s)", shared_id, len(refs), time.monotonic() - started)
        return refs

    return get_entity_files


def _build_get_file_bytes_real_helper(file_repository: FileRepositoryPort | None, loop: asyncio.AbstractEventLoop) -> Any:
    """Build the real ``get_file_bytes`` bound into the real exec namespace.

    Returns the file's raw bytes or ``None`` when the file is absent (the
    script counts it as missing and continues — mirrors best-effort
    ``move_files_to_entity``). Unwired ``file_repository`` -> a stub that
    raises a clear ``RuntimeError`` when the script calls it.
    """
    if file_repository is None:

        def get_file_bytes_unwired(filename: str) -> bytes | None:
            raise RuntimeError(
                "get_file_bytes requires a wired file_repository (got None). "
                "Wire FileRepositoryPort into the runtime/execute use case to "
                "enable supporting-file extraction."
            )

        return get_file_bytes_unwired

    def get_file_bytes(filename: str) -> bytes | None:
        started = time.monotonic()
        data = loop.run_until_complete(file_repository.get_file_bytes(filename))
        # debug, not info: same aggregate-not-per-file logging rationale as above.
        logger.debug(
            "script get_file_bytes: {} -> {} bytes ({:.1f}s)",
            filename,
            len(data) if data is not None else 0,
            time.monotonic() - started,
        )
        return data

    return get_file_bytes


def _get_segmentation_noop() -> Any:
    """Build the dummy no-op ``get_segmentation`` (dummies carry no documents)."""

    def get_segmentation(shared_id: str, language: str | None = None) -> dict | None:
        del shared_id, language  # ignored: dummies carry no primary document
        return None

    return get_segmentation


def _get_url_text_noop() -> Any:
    """Build the dummy no-op ``get_url_text`` (dummies carry no source URLs).

    The dummy gate cannot exercise external fetches (dummies are in-memory value
    objects with no URL-fetchable backing); like ``get_file_bytes`` / ``get_segmentation``
    the no-op returns ``None`` so the identical script still runs clean in the gate
    and the fetch path is only exercised live.
    """

    def get_url_text(shared_id: str, language: str | None = None) -> str | None:
        del shared_id, language  # ignored: dummies carry no source-page URL to fetch
        return None

    return get_url_text


def _build_get_segmentation_real_helper(
    segmentation_repository: SegmentationRepositoryPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str,
) -> Any:
    """Build the real ``get_segmentation`` bound into the real exec namespace.

    Resolves the entity's primary document for ``language`` and returns its
    segmentation as a plain dict (``filename``/``status``/``pages`` plus
    ``paragraphs`` carrying ``page_number``/``text``/geometry). Returns ``None``
    when the entity has no primary document or no ready segmentation
    (:class:`SegmentationNotFoundError`) — the script counts it as missing and
    continues, mirroring ``get_file_bytes``. Unwired ``segmentation_repository``
    -> a stub that raises a clear ``RuntimeError`` when the script calls it.
    """
    if segmentation_repository is None:

        def get_segmentation_unwired(shared_id: str, language: str | None = None) -> dict | None:
            raise RuntimeError(
                "get_segmentation requires a wired segmentation_repository (got None). "
                "Wire SegmentationRepositoryPort into the runtime/execute use case to "
                "enable document-page-text reading."
            )

        return get_segmentation_unwired

    def get_segmentation(shared_id: str, language: str | None = None) -> dict | None:
        lang = language or default_language
        started = time.monotonic()
        try:
            seg = loop.run_until_complete(segmentation_repository.get_by_shared_id(shared_id, lang))
        except SegmentationNotFoundError:
            return None
        result = segmentation_to_dict(seg)
        # debug, not info: same aggregate-not-per-call logging rationale as above.
        logger.debug(
            "script get_segmentation: {} -> {} paragraph(s) across {} page(s) ({:.1f}s)",
            shared_id,
            len(result["paragraphs"]),
            result["pages"],
            time.monotonic() - started,
        )
        return result

    return get_segmentation


def _build_get_url_text_real_helper(
    entity_repository: EntityRepositoryPort | None,
    url_fetcher: UrlFetcherPort | None,
    loop: asyncio.AbstractEventLoop,
    default_language: str = "en",
) -> Any:
    """Build the entity-scoped ``get_url_text`` bound into the exec namespace.

    The script passes a ``shared_id`` (never an arbitrary URL); the helper reads
    that entity's own ``link`` metadata and fetches ONLY the URL it finds. That
    removes the arbitrary-URL fetch an injected script could otherwise use for
    internal scanning or data exfiltration, while keeping Source Page extraction
    working.

    Returns ``None`` on any failure (no link, DNS/timeout, non-2xx, oversized
    body) — an external source page being down must never crash a bulk run.
    Unwired ports raise a clear ``RuntimeError`` when the script calls it.
    """
    if entity_repository is None:

        def get_url_text_no_repo(shared_id: str, language: str | None = None) -> str | None:
            raise RuntimeError(
                "get_url_text requires a wired entity repository to resolve the entity's source-page URL (got None)."
            )

        return get_url_text_no_repo

    if url_fetcher is None:

        def get_url_text_unwired(shared_id: str, language: str | None = None) -> str | None:
            raise RuntimeError(
                "get_url_text requires a wired url_fetcher (got None). "
                "Wire UrlFetcherPort into the runtime/execute use case to "
                "enable source-page HTML extraction."
            )

        return get_url_text_unwired

    def get_url_text(shared_id: str, language: str | None = None) -> str | None:
        started = time.monotonic()
        try:
            raw = loop.run_until_complete(entity_repository.get_raw_by_shared_id(shared_id, language or default_language))
            url = find_source_page_url(raw)
            if url is None:
                return None
            text = loop.run_until_complete(url_fetcher.fetch_text(url))
        except UrlFetchError as exc:
            # debug, not info: external fetches are best-effort and a full pass
            # can touch thousands of URLs.
            logger.debug("script get_url_text: {} failed: {} ({:.1f}s)", shared_id, exc, time.monotonic() - started)
            return None
        except Exception as exc:  # noqa: BLE001 — a missing/undecodable entity is "no source page"
            logger.debug("script get_url_text: cannot read {}: {} ({:.1f}s)", shared_id, exc, time.monotonic() - started)
            return None
        logger.debug(
            "script get_url_text: {} -> {} -> {} chars ({:.1f}s)",
            shared_id,
            url,
            len(text),
            time.monotonic() - started,
        )
        return text

    return get_url_text


def _build_translate_text_real_helper(translation_port: TranslationPort | None, loop: asyncio.AbstractEventLoop) -> Any:
    """Build the real ``translate_text`` bound into the real exec namespace.

    Calls :class:`TranslationPort` (Uwazi's ``POST /api/translationService``
    proxy) and PREPENDS the instance marker: the same
    ``AI_TRANSLATED_MARKER`` prefix Uwazi's own AI-translation flow appends to
    translated metadata, so a translated value reads as AI-translated in the
    UI exactly like a native Uwazi translation. The marker is part of the
    recorded value: the review table shows the marked text and Run writes it
    verbatim, so the applied metadata is already marked.

    Raises :class:`~uwazi_api.domain.exceptions.TranslationError` on failure —
    never substitute unmarked source text for a failed translation. The raise is
    PER CALL so the generated script can catch it for that one entity, skip the
    row, record the failure, and keep the rest of the pass (see the system
    prompt's PER-ENTITY FAILURE ISOLATION rule). Unwired ``translation_port``
    -> a stub that raises a clear ``RuntimeError`` when the script calls it.
    """
    if translation_port is None:

        def translate_text_unwired(text: str, language_from: str, language_to: str) -> str:
            raise RuntimeError(
                "translate_text requires a wired translation_port (got None). Wire "
                "TranslationPort into the runtime/execute use case to enable "
                "translations-endpoint translation."
            )

        return translate_text_unwired

    def translate_text(text: str, language_from: str, language_to: str) -> str:
        started = time.monotonic()
        translated = loop.run_until_complete(translation_port.translate(text, language_from, language_to))
        logger.debug(
            "script translate_text: {} -> {} -> {} chars ({:.1f}s)",
            language_from,
            language_to,
            len(translated),
            time.monotonic() - started,
        )
        return f"{AI_TRANSLATED_MARKER}{translated}"

    return translate_text


def _translate_text_noop() -> Any:
    """Build the dummy no-op ``translate_text`` (dummies are in-memory value objects).

    The dummy gate cannot exercise real translation calls (dummies carry no
    external translation service); like ``get_url_text`` the no-op returns the
    marker + the source text UNCHANGED so the identical script still runs clean
    in the gate and the recorded value shape (marker-prefixed string) matches
    what the real mode records. The real endpoint path is exercised live only.
    """

    def translate_text(text: str, language_from: str, language_to: str) -> str:
        _ = language_from, language_to  # ignored: dummies do not call the translation endpoint
        return f"{AI_TRANSLATED_MARKER}{text}"

    return translate_text
