from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.publish_state import PublishState


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
    file_language: str | None = Field(
        default=None,
        description=(
            "The file's OWN language as stored on the file row (ISO 639-3, e.g. 'eng'), "
            "distinct from the entity row language above. Used by read-only queries to tell "
            "same-named files in different languages apart; not used on restore."
        ),
    )
    content_type: str = Field(description="The MIME type for the multipart part Content-Type header.")
    restored: bool = Field(
        default=False,
        description=(
            "Whether this file has been re-uploaded to the re-created entity during "
            "a revert. Checkpointed into the snapshot so a resumed (mid-crash) "
            "revert skips already-restored files instead of re-uploading duplicates."
        ),
    )
    size: int | None = Field(
        default=None,
        description=(
            "The file's byte size, when the raw entry carries it. Not used on restore; "
            "the merge file-move uses it as the cheap duplicate-candidate key (exact "
            "identity is the content digest)."
        ),
    )


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


class EntityRowSnapshot(BaseModel):
    """One entity-language ROW's exact raw, captured at backup time.

    A sharedId has one row per configured language (Uwazi multilingual
    entities); each row carries its own ``_id``, ``language`` and per-language
    metadata values. A write to the entity's ``es`` row changes ONLY that row —
    so a revert that restores just one row's raw leaves the other languages
    modified (the partial-revert bug). A snapshot therefore captures EVERY
    configured language's row, and a revert restores each in turn.
    """

    model_config = ConfigDict(frozen=True)

    language: str = Field(description="The row's locale (ISO 639-1, e.g. 'en'); '' when unknown.")
    internal_id: str | None = Field(default=None, description="The row's Uwazi _id, when known.")
    raw: dict[str, Any] = Field(description="The exact raw entity JSON Uwazi returned for this row.")


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
    rows: list[EntityRowSnapshot] = Field(
        default_factory=list,
        description=(
            "Every entity-language ROW captured at backup time (one per configured "
            "language that has a row). ``raw``/``language`` keep the row the backup "
            "was first fetched in (legacy shape, still the delete path's re-create "
            "source); ``rows`` extends the revert to ALL languages so a write to "
            "one language's row does not leave the others diverged after revert. "
            "Empty on legacy snapshots (single-raw semantics apply)."
        ),
    )
    affected_languages: list[str] = Field(
        default_factory=list,
        description=(
            "The languages whose rows the run was about to write when this snapshot "
            "was taken (the task's affected-language set for the entity). Drives "
            "'back up the first time and keep the first backup': rows outside this "
            "set still get restored (they were captured), but the tooltip and the "
            "revert summary name the affected ones."
        ),
    )
    publish_state: PublishState | None = Field(
        default=None,
        description=(
            "The entity's publish + permission grants at capture time. `POST /api/entities` "
            "never writes `published`/`permissions` (the access policy is a separate "
            "collection), so without this a reverted publish/unpublish/permission change "
            "stayed live. None on legacy snapshots (the restore is then skipped)."
        ),
    )
    connection_hubs: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The connection rows a metadata restore CANNOT reproduce: rows carrying "
            "`file` (file-cited connections) or `reference` (text citations) instead "
            "of a relationship-type `template`. `POST /api/entities` rebuilds hubs only "
            "from relationship-property metadata, so a run that created such a row could "
            "not undo it. See domain/connection_hubs.py."
        ),
    )
