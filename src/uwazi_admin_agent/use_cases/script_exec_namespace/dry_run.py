"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.parallel_script_helpers import (
    assert_unique_move_targets,
)


def _noop_add_info(title: str, children: list | tuple | str | None = None, keep: bool = False) -> dict:
    """A record-free ``add_info`` for namespaces that build no review table.

    The dummy-validation and real-exec namespaces bind the same helper names as
    the dry-run planner so a generated script never fails on a missing name.
    Those paths have no suggestion store to write to, so the text is simply
    accepted (it has no effect on Uwazi either way).
    """
    del title, children, keep
    return {"success": True}


def _noop_record_skipped(
    shared_id: str,
    reason: str,
    *,
    title: str = "",
    template_name: str | None = None,
    detail: str | None = None,
) -> dict:
    """A record-free ``record_skipped`` for namespaces that build no table (see above)."""
    del reason, title, template_name, detail
    return {"success": True, "shared_id": shared_id}


def _dry_run_write_helpers(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the write helpers for the dry-run namespace: recorders, no I/O.

    Each helper appends one record per operation into the shared ``records``
    list and returns the same success-shaped value the real helpers return, so
    an identical script runs to completion. Nothing is sent to Uwazi: this is
    the dry-run write boundary (real reads, recorded writes).

    ``add_info`` is the display-only exception: it records the text a script
    wants the operator to READ (a query answer, a result summary) rather than a
    would-be write. The review table renders it as a two-level info operation
    with no action buttons. ``record_skipped`` is its PER-ENTITY sibling: one
    display-only row carrying the entity, a stable reason id, and optional
    evidence, so skipped entities can be listed, filtered and linked instead of
    being collapsed into one paragraph.

    The returned dict ALSO carries the records list itself under the
    underscore-prefixed key ``_dry_run_records`` (the namespace convention for
    non-helper keys) so the caller can read what was recorded after
    :func:`run_script_sync`.
    """

    def create_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        del language  # recorded per-dict, not as a language override
        logger.info("dry-run record: create_entities x{}", len(entities_dicts))
        for i, d in enumerate(entities_dicts):
            records.append(
                {
                    "op": "create",
                    # The placeholder this call returns: a later move/delete in
                    # the SAME script addresses the new entity through it, so the
                    # record has to carry it (merge detection pairs them).
                    "shared_id": f"dry-created-{i}",
                    "title": d.get("title", ""),
                    "template_name": d.get("template_name", ""),
                    "metadata": d.get("metadata", {}),
                }
            )
        return [{"shared_id": f"dry-created-{i}", "success": True} for i, d in enumerate(entities_dicts)]

    def update_entities(entities_dicts: list[dict], language: str | None = None) -> list[dict]:
        logger.info("dry-run record: update_entities x{}", len(entities_dicts))
        for d in entities_dicts:
            records.append(
                {
                    "op": "update",
                    "shared_id": d.get("shared_id"),
                    "template_name": d.get("template_name"),
                    "metadata": d.get("metadata"),
                    "title": d.get("title"),
                    "language": language,
                    "basis": [str(b) for b in d.get("basis") or []],
                }
            )
        return [{"shared_id": d.get("shared_id"), "success": True} for d in entities_dicts]

    def delete_entities(shared_ids: list[str]) -> list[dict]:
        logger.info("dry-run record: delete_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "delete", "shared_id": sid})
        return [{"shared_id": sid, "success": True} for sid in shared_ids]

    def publish_entities(shared_ids: list[str]) -> dict:
        logger.info("dry-run record: publish_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "publish", "shared_id": sid})
        return _dry_run_publish_summary(len(shared_ids))

    def unpublish_entities(shared_ids: list[str]) -> dict:
        logger.info("dry-run record: unpublish_entities x{}", len(shared_ids))
        for sid in shared_ids:
            records.append({"op": "unpublish", "shared_id": sid})
        return _dry_run_publish_summary(len(shared_ids))

    def set_publish_status(shared_ids: list[str], published: bool) -> list[dict]:
        logger.info("dry-run record: set_publish_status x{} published={}", len(shared_ids), published)
        for sid in shared_ids:
            records.append({"op": "set_publish_status", "shared_id": sid, "published": published})
        return [{"shared_id": sid, "success": True} for sid in shared_ids]

    def create_relationships(relationships_dicts: list[dict], language: str | None = None) -> list[dict]:
        del language
        logger.info("dry-run record: create_relationships x{}", len(relationships_dicts))
        for r in relationships_dicts:
            records.append({"op": "create_relationships", **r})
        return [{"success": True} for _ in relationships_dicts]

    def record_skipped(
        shared_id: str,
        reason: str,
        *,
        title: str = "",
        template_name: str | None = None,
        detail: str | None = None,
    ) -> dict:
        """Record ONE entity the script could not process (no Uwazi write).

        ``reason`` is a stable id (``no_document``, ``no_segmentation``,
        ``no_page_1``, ``no_code_in_page_1``, ``extraction_error``); the review
        table maps it to a descriptive sentence. ``detail`` carries optional
        evidence (for example the page text the extractor examined), rendered
        beneath the reason. The row is excluded from every Run/selection path.
        """
        records.append(
            {
                "op": "skipped",
                "shared_id": str(shared_id or ""),
                "title": "" if title is None else str(title),
                "template_name": template_name,
                "reason": str(reason or ""),
                "detail": None if detail is None else str(detail),
            }
        )
        logger.info("dry-run record: skipped {} reason={}", shared_id, reason)
        return {"success": True, "shared_id": shared_id}

    info_count = 0

    def add_info(title: str, children: list | tuple | str | None = None, keep: bool = False) -> dict:
        """Record arbitrary result text for the review table (no Uwazi write).

        ``title`` is the operation row's text; ``children`` (optional) is a
        list of detail lines rendered as its two-level suboperations. A single
        string is accepted as one child. Everything is coerced to text because
        the review table displays it verbatim.

        ``keep`` marks this row as information the plan's CHANGE rows cannot
        show (failed/skipped entities, or an explicitly requested answer). The
        review conversion drops info rows whenever the same script records
        changes, so a plain summary cannot bury the operations; only a kept row
        survives beside them.
        """
        nonlocal info_count
        if children is None:
            child_list: list[Any] = []
        elif isinstance(children, (list, tuple)):
            child_list = list(children)
        else:
            child_list = [children]
        shared_id = f"info-{info_count}"
        record: dict[str, Any] = {
            "op": "info",
            "shared_id": shared_id,
            "title": "" if title is None else str(title),
            "children": ["" if child is None else str(child) for child in child_list],
        }
        if keep:
            # Omitted when False so a plain info record keeps its old shape.
            record["keep"] = True
        records.append(record)
        info_count += 1
        logger.info("dry-run record: add_info {}", shared_id)
        return {"success": True, "shared_id": shared_id}

    return {
        "create_entities": create_entities,
        "update_entities": update_entities,
        "delete_entities": delete_entities,
        "publish_entities": publish_entities,
        "unpublish_entities": unpublish_entities,
        "set_publish_status": set_publish_status,
        "create_relationships": create_relationships,
        "add_info": add_info,
        "record_skipped": record_skipped,
        "_dry_run_records": records,
    }


def _dry_run_publish_summary(count: int) -> dict[str, Any]:
    """The summary-dict shape the real ``publish_entities``/``unpublish_entities`` return."""
    return {
        "success_count": count,
        "failure_count": 0,
        "rate_limited": [],
        "permission_denied": [],
        "not_found": [],
        "errors": [],
    }


def _dry_run_parallel_write_aliases(dry_writes: dict[str, Any]) -> dict[str, Any]:
    """The ``*_parallel`` write names for the dry-run namespace: recorder aliases.

    Dry-run writes are pure recorders with the sequential helpers' exact
    shapes, so the parallel names alias them: the identical script runs to
    completion and its would-be writes are recorded exactly as the sequential
    call would (per entity), with zero mutations either way.
    """
    return {
        "update_entities_parallel": dry_writes["update_entities"],
        "create_entities_parallel": dry_writes["create_entities"],
        "create_relationships_parallel": dry_writes["create_relationships"],
    }


def _dry_run_moved_files(
    entity_repository: EntityRepositoryPort | None,
    loop: asyncio.AbstractEventLoop | None,
    default_language: str,
    from_shared_ids: list[str],
    language: str | None,
) -> list[dict[str, Any]]:
    """The source file refs a dry-run move WOULD copy, best-effort (real reads).

    The review table has to show what a merge does to the primary documents, not
    just that a move exists. The move record therefore carries the file refs the
    mover would read from each source. This is a best-effort READ (the dry run
    itself moves nothing): an unwired repository or a failed source fetch leaves
    that source's list empty, and the review falls back to a per-source move row
    rather than inventing file names.
    """
    if entity_repository is None or loop is None:
        return []
    lang = language or default_language
    files: list[dict[str, Any]] = []
    for shared_id in from_shared_ids:
        try:
            raw = loop.run_until_complete(entity_repository.get_raw_by_shared_id(shared_id, lang))
        except Exception as exc:  # noqa: BLE001 — display metadata is best-effort
            logger.warning("dry-run move: could not read files of {}: {}", shared_id, exc)
            continue
        for ref in extract_file_refs(raw):
            entry = ref.model_dump()
            entry["from_shared_id"] = shared_id
            files.append(entry)
    return files


def _dry_run_move_files_helper(
    records: list[dict[str, Any]],
    entity_repository: EntityRepositoryPort | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    default_language: str = "en",
) -> Any:
    """Dry-run ``move_files_to_entity``: record the request and its file refs, move nothing."""

    def move_files_to_entity(from_shared_ids: list[str], to_shared_id: str, language: str | None = None) -> dict:
        logger.info("dry-run record: move_files_to_entity x{} -> {}", len(from_shared_ids), to_shared_id)
        records.append(
            {
                "op": "move_files",
                "from_shared_ids": list(from_shared_ids),
                "to_shared_id": to_shared_id,
                "files": _dry_run_moved_files(entity_repository, loop, default_language, list(from_shared_ids), language),
            }
        )
        return {"moved": len(from_shared_ids), "failed": 0, "skipped": 0}

    return move_files_to_entity


def _dry_run_move_files_parallel_helper(
    records: list[dict[str, Any]],
    entity_repository: EntityRepositoryPort | None = None,
    loop: asyncio.AbstractEventLoop | None = None,
    default_language: str = "en",
) -> Any:
    """Dry-run ``move_files_to_entity_parallel``: one record per move, move nothing.

    Mirrors ``_dry_run_move_files_helper`` per move so ``_count_ops`` still
    counts every would-be move as ``would_rewire``, and keeps the real
    helper's :func:`assert_unique_move_targets` guard (a rehearsal of a
    duplicated-target script must fail exactly like the execute pass would).
    The per-move summaries carry the real helper's result shape, so the SAME
    script reads either mode's return values unchanged.
    """

    def move_files_to_entity_parallel(moves: list[dict], language: str | None = None) -> list[dict]:
        assert_unique_move_targets(moves)
        logger.info("dry-run record: move_files_to_entity_parallel x{} target(s)", len(moves))
        summaries: list[dict] = []
        for move in moves:
            from_shared_ids = list(move["from_shared_ids"])
            records.append(
                {
                    "op": "move_files",
                    "from_shared_ids": from_shared_ids,
                    "to_shared_id": move["to_shared_id"],
                    "files": _dry_run_moved_files(entity_repository, loop, default_language, from_shared_ids, language),
                }
            )
            summaries.append(
                {
                    "to_shared_id": move["to_shared_id"],
                    "moved": len(from_shared_ids),
                    "failed": 0,
                    "skipped": 0,
                }
            )
        return summaries

    return move_files_to_entity_parallel


def _dry_run_query_entities_unwired() -> Any:
    """Unwired ``query_entities`` stub for the dry-run namespace (``entity_api is None``).

    Mirrors the loud-failure convention of the other real helpers' unwired
    stubs: raise a clear ``RuntimeError`` when the script actually calls it.
    """

    def query_entities_unwired(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "query_entities requires a wired entity_api (got None). Wire "
            "EntityApiPort into the runtime to enable entity discovery reads."
        )

    return query_entities_unwired


def _dry_run_full_entities_unwired() -> Any:
    """Unwired ``query_entities_full`` stub for the dry-run namespace (``entity_api is None``).

    Same loud-failure convention as :func:`_dry_run_query_entities_unwired`:
    raise a clear ``RuntimeError`` when the script actually calls it.
    """

    def query_entities_full_unwired(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(
            "query_entities_full requires a wired entity_api (got None). Wire "
            "EntityApiPort into the runtime to enable entity discovery reads."
        )

    return query_entities_full_unwired
