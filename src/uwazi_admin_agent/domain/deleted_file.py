"""The per-file revert record for run-deleted FILE rows.

Why this exists: file deletes are NOT entity writes — ``documents``/
``attachments`` are a runtime JOIN from the files collection by ``sharedId``
(``app/api/entities/entities.js`` ``withDocuments``), so nothing about a
deleted file row can ride an :class:`EntitySnapshot` (whose ``raw`` restore
can only rewrite the entity row). A run that deletes file rows therefore
needs its OWN manifest section, :attr:`MigrationManifest.deleted_files`, so
revert can re-upload each deleted file's captured bytes.

Each record carries everything revert needs to re-upload one file: the
entity ``shared_id`` it lived on (the re-upload target — or the entity's
re-created sharedId when the same run also deleted the entity), the original
``file_id`` (the backup store's bytes key, ``(run_id, shared_id, file_id)``),
the upload ``kind`` (which endpoint to call), the human-readable
``originalname`` + ``content_type`` (multipart fields), and the storage
``filename``/``size`` for traceability. ``source`` records WHICH nominator
deleted the file so the revert summary can be honest about undo semantics:
re-uploading a ``dedupe`` delete RE-CREATES a duplicate copy (the correct
undo of a dedupe cleanup, but the operator must be told plainly), while an
``explicit`` delete restores a file the operator asked to remove.

Language fidelity (accepted limit, mirroring delete-revert file restore): the
recorded ``language`` is the ENTITY row language (the upload's ``locale``
cookie), not the file row's own ``language`` — Uwazi's V2 upload path
(``FileUploadForEntity`` → ``InputFile.toEntityFile``) does not set a file-row
language from the cookie at all: documents get a content-detected ISO 639-3
language re-derived from the same bytes on re-upload, and attachments carry
none. Capturing the raw entry's own language would add no re-upload fidelity.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.snapshot import FileRef

DeleteSource = Literal["dedupe", "explicit"]


class DeletedFile(BaseModel):
    """One file row this run deleted, recorded for revert + audit."""

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The entity the file lived on — the re-upload target on revert.")
    file_id: str = Field(description="The original file _id — the backup store's bytes key + audit trace.")
    kind: Literal["document", "attachment"] = Field(description="Which upload endpoint revert calls.")
    originalname: str = Field(description="The human-readable name — the upload `originalname` body field.")
    filename: str = Field(
        description="The original storage filename (hash) — traceability + the byte-cache eviction key on delete."
    )
    language: str | None = Field(
        default=None,
        description="The entity row language (ISO 639-1) for the upload `locale` cookie; NOT the file row's own.",
    )
    content_type: str = Field(description="The MIME type for the multipart part Content-Type header.")
    size: int | None = Field(default=None, description="The file's byte size, when known (traceability).")
    source: DeleteSource = Field(
        description="Which nominator deleted the file: 'dedupe' (revert re-creates a duplicate) or 'explicit'."
    )


def to_deleted_file(shared_id: str, ref: FileRef, source: DeleteSource) -> DeletedFile:
    """Build one :class:`DeletedFile` record from a backed-up file ref.

    Pure: field copying only. Called by the helper on the script thread after
    the deletion batch joins, for deletes that SUCCEEDED (a soft-``False``
    delete left the file in place — recording it would make revert re-upload a
    copy that never went away, a duplicate).
    """
    return DeletedFile(
        shared_id=shared_id,
        file_id=ref.file_id,
        kind=ref.kind,
        originalname=ref.originalname,
        filename=ref.filename,
        language=ref.language,
        content_type=ref.content_type,
        size=ref.size,
        source=source,
    )


def to_file_ref(record: DeletedFile) -> FileRef:
    """Build a :class:`FileRef` from a record (the deleted-entity verify check).

    Pure: field copying. The post-revert verification of a re-created entity
    compares its file arrays against the FULL pre-delete file set — the
    snapshot's captured files PLUS the files the run deleted from it earlier
    (both come back on revert: the former via the recreate's file restore, the
    latter via :class:`RestoreDeletedFilesAction`) — so the records must join
    the snapshot refs under the same shape :func:`build_file_gaps` reads.
    """
    return FileRef(
        file_id=record.file_id,
        kind=record.kind,
        filename=record.filename,
        originalname=record.originalname,
        language=record.language,
        content_type=record.content_type,
        size=record.size,
    )
