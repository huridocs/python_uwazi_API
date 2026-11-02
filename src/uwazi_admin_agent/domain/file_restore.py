"""Pure transforms for delete-revert file restore (§8 changelog, file-restore phase).

When a script deletes an entity, Uwazi tears down the entity's stored file bytes
(``BulkCleanupEntityUseCase`` → ``filesService.deleteEntityFiles`` →
``deleteFilesFromStorage``). So delete-revert — which re-creates the entity via
the create branch with a fresh ``sharedId`` — must **re-upload** the original
documents and uploaded attachments to the re-created entity to restore them.
URL attachments are already restored by the create path (``create_payload.py``
keeps ``attachments``); only **uploaded** files (documents + attachments with
stored bytes, i.e. no ``url``) need re-upload.

This module holds the **pure** pieces:

- :data:`_CONTENT_TYPE_BY_EXT` — a small extension→MIME map for the multipart
  part header (default ``application/octet-stream``). Documents are always
  ``application/pdf`` (Uwazi's document upload is PDF-centric and
  ``upload_document_from_bytes`` defaults to it).
- :func:`extract_file_refs` — pull :class:`FileRef` metadata out of a snapshot
  raw (documents + uploaded attachments; URL attachments skipped).
- :func:`build_file_restore_actions` — order the upload plan (documents first so
  the primary PDF is in place, then attachments; within-kind order preserved).
- :class:`LiveFileRef` + :func:`extract_live_file_refs` — the SAME metadata read
  from a *live* raw, for the merge-revert cleanup.
- :func:`merge_copy_candidates` — pick the file rows a merge appended to its
  target. A merge re-uploads each source file (minting a NEW file id), so a raw
  restore cannot remove them: the file collection is a runtime join, not entity
  state. The candidates are selected by source identity + the run's time window;
  the caller confirms content by digest before deleting anything.

No I/O here — the backup intercept fetches + persists the bytes at backup time
(via :class:`FileRepositoryPort` + :class:`BackupStorePort`); the revert use
case loads the bytes and uploads them at revert time. This module is the
unit-test target for the file-restore decision.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.snapshot import FileRef

# Documents are always PDFs in Uwazi (the document upload endpoint is PDF-centric
# and `upload_document_from_bytes` defaults to application/pdf).
_DOCUMENT_CONTENT_TYPE: str = "application/pdf"

# Minimal extension→MIME map for uploaded attachments. The multipart part header
# carries this; Uwazi does not validate it strictly, but sending a reasonable
# value matches `FileRepository`'s expectation. Unknown extensions default to
# application/octet-stream.
_CONTENT_TYPE_BY_EXT: dict[str, str] = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "txt": "text/plain",
    "csv": "text/csv",
    "html": "text/html",
    "json": "application/json",
    "xml": "application/xml",
    "zip": "application/zip",
    "mp3": "audio/mpeg",
    "mp4": "video/mp4",
    "wav": "audio/wav",
    "rtf": "application/rtf",
    "odt": "application/vnd.oasis.opendocument.text",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "epub": "application/epub+zip",
}

_DEFAULT_CONTENT_TYPE: str = "application/octet-stream"


def _content_type_for(originalname: str) -> str:
    """Derive a MIME type from the originalname extension (default octet-stream)."""
    if not originalname:
        return _DEFAULT_CONTENT_TYPE
    dot = originalname.rfind(".")
    if dot < 0 or dot == len(originalname) - 1:
        return _DEFAULT_CONTENT_TYPE
    ext = originalname[dot + 1 :].lower()
    return _CONTENT_TYPE_BY_EXT.get(ext, _DEFAULT_CONTENT_TYPE)


def _file_id_of(entry: Any) -> str | None:
    """Return the file's _id as a string, or None if absent/blank."""
    if not isinstance(entry, dict):
        return None
    fid = entry.get("_id")
    if isinstance(fid, str) and fid:
        return fid
    return None


def extract_file_refs(raw: dict[str, Any]) -> list[FileRef]:
    """Pull :class:`FileRef` metadata for uploaded files out of a snapshot raw.

    Captures every ``raw.documents`` entry (kind=document) and every
    ``raw.attachments`` entry **without a ``url``** (kind=attachment — uploaded
    bytes; URL attachments are already restored by the create path). Entries
    missing ``_id`` or ``originalname`` are skipped (they cannot be fetched or
    meaningfully re-uploaded). ``language`` is the entity row language
    (``raw['language']``, ISO 639-1) — the upload's ``locale`` cookie; Uwazi
    derives the file row's own language from it. Multi-language-document edge
    cases (a doc whose row language differs from the entity's) are a flagged
    limitation: the doc re-uploads under the entity's primary language.

    Pure: returns a new list; the source ``raw`` is not mutated.
    """
    entity_language = raw.get("language")
    refs: list[FileRef] = []

    documents = raw.get("documents") if isinstance(raw.get("documents"), list) else []
    for doc in documents:
        ref = _ref_from_entry(doc, kind="document", entity_language=entity_language)
        if ref is not None:
            refs.append(ref)

    attachments = raw.get("attachments") if isinstance(raw.get("attachments"), list) else []
    for att in attachments:
        # URL attachments have no stored bytes (restored by create); skip them.
        if isinstance(att, dict) and att.get("url"):
            continue
        ref = _ref_from_entry(att, kind="attachment", entity_language=entity_language)
        if ref is not None:
            refs.append(ref)

    return refs


def _ref_from_entry(entry: Any, *, kind: Literal["document", "attachment"], entity_language: Any) -> FileRef | None:
    """Build a :class:`FileRef` from one documents/attachments entry, or None."""
    if not isinstance(entry, dict):
        return None
    file_id = _file_id_of(entry)
    originalname = entry.get("originalname")
    if file_id is None or not isinstance(originalname, str) or not originalname:
        return None
    filename = entry.get("filename")
    filename_str = filename if isinstance(filename, str) and filename else file_id
    content_type = _DOCUMENT_CONTENT_TYPE if kind == "document" else _content_type_for(originalname)
    language = entity_language if isinstance(entity_language, str) and entity_language else None
    file_language = entry.get("language")
    file_language = file_language if isinstance(file_language, str) and file_language else None
    size = entry.get("size")
    return FileRef(
        file_id=file_id,
        kind=kind,
        filename=filename_str,
        originalname=originalname,
        language=language,
        file_language=file_language,
        content_type=content_type,
        size=size if isinstance(size, int) else None,
    )


class FileRestoreAction(BaseModel):
    """One file to re-upload to a re-created entity during delete-revert.

    ``file_id`` is the *original* file's _id — the key the backup store indexes
    the captured bytes under (loaded via ``(run_id, old_shared_id, file_id)``).
    The upload targets the re-created entity's *new* ``sharedId`` (supplied by
    the revert use case at runtime, not carried here).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["upload_document", "upload_attachment"] = Field(description="Which upload endpoint to call.")
    file_id: str = Field(description="The original file _id — the backup-store bytes key + audit trace.")
    originalname: str = Field(description="The human-readable name — sent as the upload `originalname` body field.")
    language: str | None = Field(
        default=None, description="The entity row language (ISO 639-1) for the upload `locale` cookie."
    )
    content_type: str = Field(description="The MIME type for the multipart part Content-Type header.")


def build_file_restore_actions(file_refs: list[FileRef]) -> list[FileRestoreAction]:
    """Order the upload plan from captured :class:`FileRef` metadata.

    Documents first (so the primary PDF is in place before attachments), then
    uploaded attachments; within-kind ordering is preserved from the snapshot.
    Pure: no I/O.
    """
    actions: list[FileRestoreAction] = []
    for ref in file_refs:
        if ref.kind == "document":
            actions.append(_to_action(ref, kind="upload_document"))
    for ref in file_refs:
        if ref.kind == "attachment":
            actions.append(_to_action(ref, kind="upload_attachment"))
    return actions


def _to_action(ref: FileRef, *, kind: Literal["upload_document", "upload_attachment"]) -> FileRestoreAction:
    return FileRestoreAction(
        kind=kind,
        file_id=ref.file_id,
        originalname=ref.originalname,
        language=ref.language,
        content_type=ref.content_type,
    )


# The merge file-move and the revert run share one clock (both run against the
# same instance from the same deployment), so this only forgives a few minutes of
# clock drift when deciding "this row was created during the run". It never
# widens the window enough to excuse an old file.
_MERGE_CLOCK_SKEW_MS: int = 5 * 60 * 1000


class LiveFileRef(BaseModel):
    """One content file row a LIVE raw currently joins (a merge-copy candidate).

    Mirrors :class:`FileRef` for the live side of a merge revert: the row's
    ``_id`` is the file to delete, ``filename`` is what
    :meth:`FileRepositoryPort.get_file_bytes` fetches, and
    ``creation_date_ms`` is Uwazi's server-side ``creationDate`` (epoch ms),
    the signal that tells a copy the merge just uploaded apart from a file
    that was already there.
    """

    model_config = ConfigDict(frozen=True)

    file_id: str
    kind: Literal["document", "attachment"]
    filename: str
    originalname: str
    size: int | None = None
    creation_date_ms: int | None = None


def _live_ref(entry: Any, *, kind: Literal["document", "attachment"]) -> LiveFileRef | None:
    """Build a :class:`LiveFileRef` from one documents/attachments entry, or None."""
    if not isinstance(entry, dict):
        return None
    file_id = _file_id_of(entry)
    originalname = entry.get("originalname")
    filename = entry.get("filename")
    if file_id is None or not isinstance(originalname, str) or not originalname:
        return None
    if not isinstance(filename, str) or not filename:
        return None
    size = entry.get("size")
    creation_date = entry.get("creationDate")
    creation_date_ms = creation_date if isinstance(creation_date, int) and not isinstance(creation_date, bool) else None
    return LiveFileRef(
        file_id=file_id,
        kind=kind,
        filename=filename,
        originalname=originalname,
        size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        creation_date_ms=creation_date_ms,
    )


def extract_live_file_refs(raw: dict[str, Any]) -> list[LiveFileRef]:
    """Pull :class:`LiveFileRef` for the content files a live raw joins.

    Same scope as :func:`extract_file_refs` — ``documents`` plus uploaded
    ``attachments`` (an attachment with a ``url`` stores no bytes and cannot be
    a merge copy) — but reads the *live* entries, whose ``_id``/``filename`` are
    the ones the merge minted and whose ``creationDate`` dates the upload.
    Pure: returns a new list; ``raw`` is not mutated.
    """
    refs: list[LiveFileRef] = []
    documents = raw.get("documents") if isinstance(raw.get("documents"), list) else []
    for doc in documents:
        ref = _live_ref(doc, kind="document")
        if ref is not None:
            refs.append(ref)
    attachments = raw.get("attachments") if isinstance(raw.get("attachments"), list) else []
    for att in attachments:
        if isinstance(att, dict) and att.get("url"):
            continue
        ref = _live_ref(att, kind="attachment")
        if ref is not None:
            refs.append(ref)
    return refs


def merge_copy_candidates(
    live_raw: dict[str, Any],
    *,
    source_files: list[FileRef],
    before_file_ids: set[str] | None,
    created_after_ms: int | None,
    created_before_ms: int | None,
) -> list[LiveFileRef]:
    """The live file rows a merge likely appended to its target (pure).

    A merge's file move re-uploads each source's primary document/uploaded
    attachment to the target, so the copy keeps the source's kind, originalname
    and size but gets a NEW file id/filename. This selects the rows worth
    byte-verifying; the caller must still confirm the content digest against
    the source's backed-up bytes before deleting anything (a same-named,
    same-sized file can differ).

    The no-loss rules:

    - a file id recorded on the target's ``before_file_ids`` (a whole-entity
      snapshot's own documents/attachments and captured file refs) is NEVER a
      candidate — it existed before the run;
    - with no before-set (``None``: a property-scoped snapshot has no file
      list, and a file-only merge target has no snapshot at all), the row must
      carry a ``creationDate`` inside the run window. Anything older (an
      operator's pre-existing file) or newer (an upload after the sources were
      deleted) is kept;
    - the identity must match a source file's ``(kind, originalname, size)``;
      a missing size on either side only loosens that check, never the digest
      check the caller performs.

    Pure: no I/O. ``created_after_ms``/``created_before_ms`` are epoch ms; a
    ``None`` bound is unbounded. The clock-skew margin is only applied to the
    bounds, never to identity.
    """
    sizes_by_name: dict[tuple[str, str], set[int | None]] = {}
    for ref in source_files:
        sizes_by_name.setdefault((ref.kind, ref.originalname), set()).add(ref.size)

    candidates: list[LiveFileRef] = []
    for ref in extract_live_file_refs(live_raw):
        if before_file_ids is not None and ref.file_id in before_file_ids:
            continue
        known_sizes = sizes_by_name.get((ref.kind, ref.originalname))
        if not known_sizes:
            continue
        if ref.size is not None and None not in known_sizes and ref.size not in known_sizes:
            continue
        if ref.creation_date_ms is None:
            # Without a timestamp, only a recorded before-set can prove the id
            # is new. No before-set means an unverifiable row: keep it.
            if before_file_ids is None:
                continue
            candidates.append(ref)
            continue
        if created_after_ms is not None and ref.creation_date_ms < created_after_ms - _MERGE_CLOCK_SKEW_MS:
            continue
        if created_before_ms is not None and ref.creation_date_ms > created_before_ms + _MERGE_CLOCK_SKEW_MS:
            continue
        candidates.append(ref)
    return candidates
