"""Pure post-revert verification (§5 Phase 6).

After :class:`RevertRunUseCase` restores, the operator must be able to confirm
the restore actually matched the snapshots — not just trust the revert. This
module holds the **pure** decision: given the manifest, the loaded snapshots,
and the *current* raws fetched post-revert, decide per entity whether the
restore matches. This is the unit-test target named by the Phase 6 DoD
("revert-verification decisions").

Comparison semantics mirror the Phase-3 gate (:mod:`domain.validation_result`):
entity-raw equality excludes :data:`PLATFORM_MANAGED_FIELDS` (``editDate`` is
server-managed and advances on every save, including the revert save — see the
§8 changelog). Backup/restore still preserve those fields in the snapshot (raw
fidelity, §2.5); only the *comparison* ignores them.

Three checks:
- **modified** entities: ``current_raw`` (excl. platform-managed)
  equals ``snapshot.raw`` (excl. platform-managed). Identity (_id/sharedId) must
  match too (modified entities keep their identity on the update branch).
- **deleted** entities: re-created via the create branch, so Uwazi minted a
  fresh _id/sharedId. The comparison excludes platform-managed, identity, and
  file fields (:data:`IDENTITY_FIELDS`, :data:`FILE_FIELDS`) — only DATA fields
  are expected to match. A dedicated file-gap check (:func:`build_file_gaps`)
  compares the snapshot's captured files against the re-created entity's
  documents/attachments by originalname+kind to flag missing/extra files. The
  current raw is fetched by the recorded ``restored_shared_id``; a ``None``
  actual means the re-create failed (the old id is gone).
- **rewired** relationships: ``current_raw[<property_name>]`` equals the
  recorded ``before``. Rewired from-entities are also in ``modified`` (so the
  full-raw check already covers ``relations``), but the plan calls out
  relationships explicitly — a distinct mismatch is emitted when the field
  differs (belt-and-suspenders, cheap).
- **created** entities: ``current_raw`` is ``None`` (the revert deleted them).
  A present raw is a mismatch (created entity survived the revert).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.manifest import MigrationManifest
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef
from uwazi_admin_agent.domain.validation_result import FILE_FIELDS, IDENTITY_FIELDS, PLATFORM_MANAGED_FIELDS

MismatchKind = Literal["entity", "relationship", "created"]


def _strip_platform_managed(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without :data:`PLATFORM_MANAGED_FIELDS` (``None`` passes through)."""
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if k not in PLATFORM_MANAGED_FIELDS}


def _strip_for_recreate(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without platform-managed, identity, and file fields.

    For a deleted entity that was re-created via the create branch, Uwazi minted
    a fresh _id/sharedId, so those differ by design and must be excluded from the
    comparison (only the data fields are expected to match). The
    ``documents``/``attachments`` arrays are denormalized views of the `files`
    collection and are re-minted on re-upload (fresh file _ids/filenames), so
    they can never match by identity — they are excluded here and a dedicated
    :func:`build_file_gaps` check compares by originalname+kind to detect actual
    file-restore gaps. Modified entities keep their identity and their files, so
    they use the stricter _strip_platform_managed.
    """
    if raw is None:
        return None
    skip = PLATFORM_MANAGED_FIELDS | IDENTITY_FIELDS | FILE_FIELDS
    return {k: v for k, v in raw.items() if k not in skip}


class VerificationMismatch(BaseModel):
    """One entity whose post-revert state did not match its recorded before-state."""

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The entity that did not match.")
    kind: MismatchKind = Field(
        description="'entity' (modified/deleted raw mismatch), 'relationship' "
        "(rewired field mismatch), or 'created' (created entity still present)."
    )
    expected: Any = Field(description="The recorded before-state (snapshot raw, before, or None).")
    actual: Any = Field(description="The current post-revert state (raw, field value, or None/present).")


class FileGap(BaseModel):
    """One file the snapshot expected but the re-created entity is missing (or extra).

    ``kind`` is ``missing`` when the snapshot captured a file but the
    re-created entity has no matching document/attachment by originalname+kind,
    or ``extra`` when the re-created entity has a file the snapshot did not
    capture. The dedicated file check emits one gap per mismatched file so the
    operator sees exactly which files did not come back.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The re-created entity whose file set did not match.")
    gap: Literal["missing", "extra"] = Field(description="Whether the file is missing or unexpected.")
    originalname: str = Field(description="The file's human-readable name.")
    kind: Literal["document", "attachment"] = Field(description="The file kind that did not match.")


class RevertVerificationResult(BaseModel):
    """Outcome of verifying a run's revert against its snapshots."""

    ok: bool = Field(description="True if every checked entity matched (no mismatches).")
    checked: int = Field(description="Number of entities checked.")
    mismatches: list[VerificationMismatch] = Field(
        default_factory=list, description="Per-entity mismatches (empty when ok)."
    )
    file_gaps: list[FileGap] = Field(
        default_factory=list,
        description=(
            "Per-file gaps for re-created (deleted) entities: files the snapshot "
            "captured but the re-created entity is missing, or unexpected files. "
            "Empty when every captured file was re-uploaded and no extras appeared."
        ),
    )


def verify_revert(
    manifest: MigrationManifest,
    snapshots: dict[str, EntitySnapshot],
    current_raws: dict[str, dict[str, Any] | None],
) -> RevertVerificationResult:
    """Pure: compare post-revert current raws against snapshots + before-states.

    ``snapshots`` keys the :class:`EntitySnapshot` for each modified/deleted
    entity (loaded via the backup store). ``current_raws`` keys the *current*
    raw for each checked entity, fetched post-revert via the entity repository;
    a ``None`` value means the entity is absent (deleted by the revert or
    never restored). For modified/deleted entities a missing snapshot is a
    loud error — it propagates (no silent skip), matching ``build_revert_actions``.
    matches ``build_revert_actions``.
    """
    mismatches: list[VerificationMismatch] = []
    file_gaps: list[FileGap] = []
    checked = 0

    for modified in manifest.modified:
        checked += 1
        snap = snapshots[modified.shared_id]
        actual = current_raws.get(modified.shared_id)
        if _strip_platform_managed(actual) != _strip_platform_managed(snap.raw):
            mismatches.append(
                VerificationMismatch(shared_id=modified.shared_id, kind="entity", expected=snap.raw, actual=actual)
            )

    for deleted in manifest.deleted:
        checked += 1
        snap = snapshots[deleted.shared_id]
        actual = current_raws.get(deleted.shared_id)
        # A deleted entity was re-created via the create branch, so its _id/sharedId
        # are fresh by design and its documents/attachments are re-minted on re-upload
        # — compare DATA fields only (excl. platform-managed, identity, and file
        # fields). A None actual means the re-create failed (the old id is gone).
        if _strip_for_recreate(actual) != _strip_for_recreate(snap.raw):
            mismatches.append(
                VerificationMismatch(shared_id=deleted.shared_id, kind="entity", expected=snap.raw, actual=actual)
            )
        # File-restore gap check: compare the snapshot's captured files against
        # the re-created entity's documents/attachments by originalname+kind (file
        # identity is re-minted, so compare by data). A None actual (re-create
        # failed) has no file set to compare — the entity mismatch above already
        # flags it.
        if actual is not None and snap.files:
            file_gaps.extend(build_file_gaps(deleted.shared_id, snap.files, actual))

    for rewired in manifest.rewired:
        sid = rewired.entity.shared_id
        actual_raw = current_raws.get(sid)
        actual_field = actual_raw.get(rewired.property_name) if isinstance(actual_raw, dict) else None
        if actual_field != rewired.before:
            mismatches.append(
                VerificationMismatch(shared_id=sid, kind="relationship", expected=rewired.before, actual=actual_field)
            )

    for created in manifest.created:
        checked += 1
        actual = current_raws.get(created.shared_id)
        if actual is not None:
            mismatches.append(
                VerificationMismatch(shared_id=created.shared_id, kind="created", expected=None, actual=actual)
            )

    ok = not mismatches and not file_gaps
    return RevertVerificationResult(ok=ok, checked=checked, mismatches=mismatches, file_gaps=file_gaps)


# --- file-restore gap check --------------------------------------------------


def _file_signature(originalname: Any, kind: Literal["document", "attachment"]) -> tuple[str, str]:
    """The identity key for comparing a captured file vs a re-uploaded file.

    File _ids/filenames are re-minted on re-upload, so the comparison keys on
    the human-readable ``originalname`` + the file ``kind``. Language is
    intentionally **not** part of the key: a captured :class:`FileRef` carries
    the entity row language (ISO 639-1, e.g. ``en``) while a re-uploaded file
    row carries the ISO 639-3 code Uwazi derives from the upload ``locale``
    cookie (e.g. ``eng``) — the codes never align, so including language would
    produce false gaps. originalname+kind is precise enough to detect a missing
    or unexpected file.
    """
    name = originalname if isinstance(originalname, str) else ""
    return (name, kind)


def build_file_gaps(
    shared_id: str,
    expected_refs: list[FileRef],
    actual_raw: dict[str, Any],
) -> list[FileGap]:
    """Pure: compare captured file refs against a re-created entity's file arrays.

    Matches by ``originalname`` + ``kind`` (file identity is re-minted on
    re-upload, and language codes do not align across the ISO 639-1/639-3 split
    — see :func:`_file_signature`). Emits one :class:`FileGap` per missing
    captured file and per unexpected re-uploaded file. URL attachments on the
    re-created entity are excluded from the ``extra`` check (they are restored
    by the create path, not by re-upload). Pure: no I/O.
    """
    expected: list[tuple[str, str]] = [_file_signature(ref.originalname, ref.kind) for ref in expected_refs]

    actual: list[tuple[str, str]] = []
    for doc in actual_raw.get("documents", []) if isinstance(actual_raw.get("documents"), list) else []:
        if isinstance(doc, dict) and isinstance(doc.get("originalname"), str):
            actual.append(_file_signature(doc.get("originalname"), "document"))
    for att in actual_raw.get("attachments", []) if isinstance(actual_raw.get("attachments"), list) else []:
        if isinstance(att, dict) and isinstance(att.get("originalname"), str):
            # URL attachments are restored by the create path, not by re-upload —
            # they are not part of the captured-file comparison.
            if att.get("url"):
                continue
            actual.append(_file_signature(att.get("originalname"), "attachment"))

    gaps: list[FileGap] = []
    for exp_name, exp_kind in expected:
        if (exp_name, exp_kind) not in actual:
            gaps.append(FileGap(shared_id=shared_id, gap="missing", originalname=exp_name, kind=exp_kind))
    for act_name, act_kind in actual:
        if (act_name, act_kind) not in expected:
            gaps.append(FileGap(shared_id=shared_id, gap="extra", originalname=act_name, kind=act_kind))
    return gaps
