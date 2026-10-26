"""File-set drift for the entities a run actually moves files from (pure).

``editDate`` cannot see a supporting file: uploading a file attaches a new file
row to the entity but does not necessarily save the entity itself, so Uwazi's
server-managed ``editDate`` stays put. The drift probe therefore cannot rely on
it to know that a merge/move source gained an attachment.

This module supplies the pure half of the targeted alternative: the file
snapshot a review row already carries (``merge_files``, recorded by the dry-run
move) is compared against the live file references of the SAME source entities.
Only sources named by a file operation are involved, so the comparison stays
bounded by the run's file work instead of scanning every entity.

A file's identity is its stored ``_id`` (Uwazi mints a fresh one on every
upload and never rewrites a stored file), with the storage/human filename as a
legacy fallback. Kind is part of the identity so a document and an attachment
with the same name never cancel each other out. Set comparison is deliberate:
the table shows which files exist, not their order or duplicate count.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def file_identity(file: Mapping[str, Any]) -> tuple[str, str]:
    """The stable identity of one uploaded file: ``(kind, file_id-or-name)`` (pure).

    Live refs carry ``file_id``; dry-run snapshots carry the same key. Legacy
    snapshots without it fall back to the storage ``filename`` and then the
    human ``originalname`` so they can still be compared. URL attachments are
    not files with stored bytes and are expected to be absent from both sides.
    """
    kind = str(file.get("kind") or file.get("type") or "")
    identifier = str(file.get("file_id") or file.get("_id") or file.get("filename") or file.get("originalname") or "")
    return kind, identifier


def file_signature(files: Iterable[Mapping[str, Any]]) -> frozenset[tuple[str, str]]:
    """The unordered identity set of a file list (pure; nameless entries ignored)."""
    return frozenset(identity for file in files if (identity := file_identity(file))[1])


def recorded_file_signatures(rows: Iterable[Any]) -> dict[str, frozenset[tuple[str, str]]]:
    """``source sharedId -> recorded file signature`` for a run's operation rows (pure).

    ``rows`` are review suggestions; only their ``merge_source_ids`` and
    ``merge_files`` matter. Every named source gets an entry even when no file
    was recorded (the snapshot says "no files"), so a later upload is visible.
    A file without ``from_shared_id`` is attributed to the row's only source
    (older records), and is skipped when the row names several.
    """
    collected: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        sources = [str(source) for source in getattr(row, "merge_source_ids", []) if source]
        for source in sources:
            collected.setdefault(source, set())
        for file in getattr(row, "merge_files", []):
            if not isinstance(file, Mapping):
                continue
            source = str(file.get("from_shared_id") or "")
            if not source and len(sources) == 1:
                source = sources[0]
            if not source:
                continue
            identity = file_identity(file)
            if not identity[1]:
                continue
            collected.setdefault(source, set()).add(identity)
    return {source: frozenset(files) for source, files in collected.items()}


def recorded_file_languages(rows: Iterable[Any]) -> dict[str, frozenset[str]]:
    """``source sharedId -> the languages its recorded files were READ in`` (pure).

    The dry-run records every source file with the entity row language it came
    from (``FileRef.language``). The live comparison must read those SAME rows:
    a multi-language entity stores a different document set per language row, so
    a move that only read one row must not look stale just because another row
    holds files the table never planned to copy (that would be a permanent false
    "files changed"). Sources with no recorded files are absent — the caller
    falls back to the run's default language, which is what a language-less
    read recorded from.
    """
    collected: dict[str, set[str]] = {}
    for row in rows:
        sources = [str(source) for source in getattr(row, "merge_source_ids", []) if source]
        for file in getattr(row, "merge_files", []):
            if not isinstance(file, Mapping):
                continue
            source = str(file.get("from_shared_id") or "")
            if not source and len(sources) == 1:
                source = sources[0]
            language = str(file.get("language") or "")
            if not source or not language:
                continue
            collected.setdefault(source, set()).add(language)
    return {source: frozenset(languages) for source, languages in collected.items()}


def changed_file_entities(
    recorded: Mapping[str, frozenset[tuple[str, str]]],
    live: Mapping[str, Iterable[Mapping[str, Any]]],
) -> list[str]:
    """The sources whose live file set differs from the recorded snapshot (pure).

    Order follows ``recorded`` (row/operation order), so the drift report is
    deterministic. A source missing from ``live`` compares as an empty file set;
    a failed live read is handled by the caller (it must not be passed here as
    "no files", or it would look like every file was deleted).
    """
    return [source for source, signature in recorded.items() if file_signature(live.get(source, ())) != signature]
