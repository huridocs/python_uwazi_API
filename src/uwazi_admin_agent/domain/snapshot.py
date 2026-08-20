from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class FileRef(BaseModel):
    """Metadata for one uploaded file captured into the backup (delete-revert file restore).

    Carries the *metadata* needed to re-upload the file to a re-created entity:
    the original file ``_id`` (the key the backup store indexes the bytes under),
    the upload ``kind`` (document vs uploaded attachment), the storage
    ``filename`` (the hash Uwazi generated — kept for traceability, not used on
    re-upload since Uwazi mints a fresh one), the human-readable ``originalname``
    (sent as the upload's ``originalname`` body field), the entity row
    ``language`` (the ISO 639-1 code used as the upload's ``locale`` cookie), and
    the ``content_type`` for the multipart part header.

    The bytes themselves live in the backup store as a parallel binary artifact
    keyed by ``(run_id, shared_id, file_id)`` — NOT embedded here, so the
    snapshot JSON stays human-readable and raw fidelity (§2.5) is preserved (the
    ``raw`` dict is untouched; ``files`` is an additive metadata list).
    """

    model_config = ConfigDict(frozen=True)

    file_id: str = Field(description="The original Uwazi file _id — the backup-store bytes key + audit trace.")
    kind: Literal["document", "attachment"] = Field(description="Which upload endpoint to use on restore.")
    filename: str = Field(description="The original storage filename (hash) — traceability only.")
    originalname: str = Field(description="The human-readable name — sent as the upload `originalname` body field.")
    language: str | None = Field(
        default=None, description="The entity row language (ISO 639-1) for the upload `locale` cookie."
    )
    content_type: str = Field(description="The MIME type for the multipart part Content-Type header.")


class EntityIdentity(BaseModel):
    """Lightweight identity on an entity, used in manifest entries.

    ``shared_id`` is required - Uwazi always assigns a ``sharedId``; a missing
    one is a loud error, not a slient ``None`` (§8 changelog).
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The Uwazi sharedId; required.")
    internal_id: str | None = Field(default=None, description="The Uwazi _id, when known.")
    language: str | None = Field(default=None, description="The row locale, when known.")
    restored_shared_id: str | None = Field(
        default=None,
        description=(
            "For a deleted entity, the NEW sharedId Uwazi minted when revert "
            "re-created it via the create branch. Set by RevertRunUseCase after "
            "re-create so post-revert verification can fetch the re-created row; "
            "None for modified/created entries and for not-yet-reverted runs."
        ),
    )


class EntitySnapshot(BaseModel):
    """The exact raw entity JSON for one entity, captured at backup time.

    ``raw`` is the unmodified dict Uwazi returned - never a validated model -
    so round-tripping drops no fields (§2.5). Identity fields are carried
    directly (not via an embedded ``EntityIdentity``) so a snapshot is
    self-contained for restore.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The Uwazi sharedId; required (§8 changelog).")
    internal_id: str | None = Field(default=None, description="The Uwazi _id, when known.")
    language: str | None = Field(default=None, description="The row locale, when known.")
    raw: dict[str, Any] = Field(description="The exact raw entity JSON Uwazi returned.")
    captured_at: datetime = Field(description="When the snapshot was captured.")
    files: list[FileRef] | None = Field(
        default=None,
        description=(
            "Metadata for uploaded files (documents + uploaded attachments) captured "
            "into the backup before a delete, so delete-revert can re-upload them to "
            "the re-created entity. None for modified/rewire snapshots (files are not "
            "torn down there) and for pre-change manifests → revert skips file restore."
        ),
    )
