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
from uwazi_admin_agent.domain.relationship_restore import (
    CapturedHub,
    InboundRef,
    extract_inbound_refs_from_existing,
    extract_mutual_deleted_hubs,
    remap_metadata_refs,
)
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef
from uwazi_admin_agent.domain.validation_result import FILE_FIELDS, IDENTITY_FIELDS, PLATFORM_MANAGED_FIELDS

MismatchKind = Literal["entity", "relationship", "created"]


def _strip_platform_managed(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without :data:`PLATFORM_MANAGED_FIELDS` (``None`` passes through)."""
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if k not in PLATFORM_MANAGED_FIELDS}


def _strip_for_recreate(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without platform-managed, identity, file, and relations fields.

    For a deleted entity that was re-created via the create branch, Uwazi minted
    a fresh _id/sharedId, so those differ by design and must be excluded from the
    comparison (only the data fields are expected to match). The
    ``documents``/``attachments`` arrays are denormalized views of the `files`
    collection and are re-minted on re-upload (fresh file _ids/filenames), so
    they can never match by identity — they are excluded here and a dedicated
    :func:`build_file_gaps` check compares by originalname+kind to detect actual
    file-restore gaps. ``relations`` is likewise a read-only denormalized view of
    the ``connections`` collection (``getByDocument``): its hub ids and endpoint
    sharedIds are re-minted/re-derived on re-create, so it is excluded here and a
    dedicated :func:`build_relationship_gaps` check verifies the mutual-deleted
    hubs came back (by remapped endpoints + type). Modified entities keep their
    identity, files, and relationships, so they use the stricter
    :func:`_strip_platform_managed` (``relations`` IS compared for them).
    """
    if raw is None:
        return None
    skip = PLATFORM_MANAGED_FIELDS | IDENTITY_FIELDS | FILE_FIELDS | {"relations"}
    return {k: v for k, v in raw.items() if k not in skip}


def _remap_for_recreate(raw: dict[str, Any] | None, id_map: dict[str, str]) -> dict[str, Any] | None:
    """Return ``raw`` with in-metadata relationship refs remapped via ``id_map``.

    Wraps :func:`remap_metadata_refs` on the ``metadata`` field only (the rest of
    the raw is untouched). Used before :func:`_strip_for_recreate` so a deleted
    entity's snapshot ref to a co-deleted entity (OLD sharedId) compares equal to
    the re-created entity's ref (NEW sharedId, re-populated by the bulk
    re-create's ``updateEntitiesMetadataByHub``). ``None`` passes through. Pure.
    """
    if raw is None or not id_map:
        return raw
    copy = dict(raw)
    copy["metadata"] = remap_metadata_refs(raw.get("metadata", {}) or {}, id_map)
    return copy


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


class RelationshipGap(BaseModel):
    """One mutual-deleted relationship hub that did not come back after revert.

    Emitted when the snapshot captured a hub between two deleted entities but
    the re-created entities' ``relations`` show no matching hub post-revert (the
    bulk re-create was skipped or failed). Keyed by the re-created (new) endpoint
    sharedIds + the relation type, since the hub id is re-minted on re-create
    (like file ids) and cannot be compared by identity.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The re-created (from-side) entity whose hub did not come back.")
    gap: Literal["missing"] = Field(default="missing", description="The hub is absent post-revert.")
    from_shared_id: str = Field(description="The re-created FROM endpoint's NEW sharedId.")
    to_shared_id: str = Field(description="The re-created TO endpoint's NEW sharedId.")
    relation_type: str = Field(description="The relation-type id the hub should carry.")


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
    relationship_gaps: list[RelationshipGap] = Field(
        default_factory=list,
        description=(
            "Per-hub gaps for re-created (deleted) entities that were mutually "
            "related: relationship hubs the snapshot captured but the re-created "
            "entities do not have post-revert (the bulk re-create was skipped or "
            "failed). Empty when every mutual-deleted hub was re-created."
        ),
    )


def _hint(value: Any) -> str:
    """Compact one-line hint of an expected/actual value for the summary."""
    if value is None:
        return "None"
    if isinstance(value, dict):
        return f"dict({', '.join(f'{k}={value[k]!r}' for k in sorted(value))})" if value else "{}"
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def format_verification_result(result: RevertVerificationResult) -> str:
    """Pure: compact multi-line summary of a post-revert verification outcome.

    One line per mismatch (kind + shared_id + expected/actual hint) and one per
    file gap; used verbatim as the operator-facing error detail.
    """
    lines = [
        f"revert verification failed ({len(result.mismatches)} mismatch(es), {len(result.file_gaps)} file gap(s), checked={result.checked})"
    ]
    for m in result.mismatches:
        lines.append(f"- {m.kind} {m.shared_id}: expected {_hint(m.expected)}, got {_hint(m.actual)}")
    for g in result.file_gaps:
        lines.append(f"- file {g.gap} {g.kind} {g.originalname!r} on {g.shared_id}")
    return "\n".join(lines)


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
    relationship_gaps: list[RelationshipGap] = []
    checked = 0

    # Old→new sharedId map for re-created deleted entities. Used to remap
    # in-metadata relationship-property refs before comparing a deleted entry's
    # snapshot vs its re-created current raw (the ref's value is a sharedId that
    # was re-minted on re-create, so it differs by design unless remapped).
    id_map: dict[str, str] = {e.shared_id: e.restored_shared_id for e in manifest.deleted if e.restored_shared_id}

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
        # fields). In-metadata relationship-property refs to co-deleted entities
        # are remapped via id_map first: the snapshot carries the OLD sharedId as
        # the ref value while the re-created entity carries the NEW sharedId (the
        # bulk re-create's updateEntitiesMetadataByHub re-populated it); remapping
        # the snapshot's ref makes the two comparable. A None actual means the
        # re-create failed (the old id is gone).
        expected_cmp = _strip_for_recreate(_remap_for_recreate(snap.raw, id_map))
        actual_cmp = _strip_for_recreate(_remap_for_recreate(actual, id_map))
        if expected_cmp != actual_cmp:
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

    # Mutual-deleted relationship hubs: the snapshot captured hubs between
    # deleted entities; revert should have re-created them (with remapped
    # endpoints) via ReapplyRelationshipRefsAction's re-created-entity re-save.
    # build_relationship_gaps checks each re-created entity's relations for the
    # expected direction-aware hub. A hub whose endpoints were not both
    # re-created is skipped (the entity mismatch above already flags the failed
    # re-create).
    deleted_ids = {e.shared_id for e in manifest.deleted}
    deleted_snapshots = {sid: snapshots[sid] for sid in deleted_ids if sid in snapshots}
    expected_hubs = extract_mutual_deleted_hubs(deleted_snapshots, deleted_ids)
    if expected_hubs:
        relationship_gaps.extend(build_relationship_gaps(expected_hubs, id_map, current_raws))

    # Inbound refs from still-existing entities to deleted ones: the delete
    # cascade (deleteReferencesToSharedIds) stripped these refs; revert's
    # ReapplyRelationshipRefsAction should have re-added them (remapped to the
    # NEW sharedId) on the still-existing entity. build_inbound_ref_gaps checks
    # each still-existing entity's relations for the restored direction-aware
    # hub. Existing entities that are themselves in the manifest are excluded —
    # that stale-id edge case is a documented limitation.
    manifest_ids = (
        {e.shared_id for e in manifest.modified}
        | {e.shared_id for e in manifest.deleted}
        | {e.shared_id for e in manifest.created}
    )
    inbound_refs = extract_inbound_refs_from_existing(deleted_snapshots, deleted_ids, excluded_existing=manifest_ids)
    if inbound_refs:
        relationship_gaps.extend(build_inbound_ref_gaps(inbound_refs, id_map, current_raws))

    ok = not mismatches and not file_gaps and not relationship_gaps
    return RevertVerificationResult(
        ok=ok,
        checked=checked,
        mismatches=mismatches,
        file_gaps=file_gaps,
        relationship_gaps=relationship_gaps,
    )


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


# --- mutual-relationship-restore gap check ----------------------------------


def _hub_exists(
    relations: list[Any],
    new_from: str,
    new_to: str,
    relation_type: str,
) -> bool:
    """Pure: does ``relations`` contain a hub matching the remapped from→to direction + type?

    The hub id is re-minted on re-create, so the match is by DIRECTION, not just
    endpoint set: a hub matches ``new_from → new_to`` of type ``relation_type``
    iff it has a FROM row ``{entity: new_from, template: null}`` AND a TO row
    ``{entity: new_to, template: relation_type}``. Direction-awareness is required
    to distinguish A'→B' from B'→A' (both share the endpoint set ``{A', B'}``);
    a direction-unaware check would false-pass a missing B'→A' hub. The
    denormalized ``relations`` view preserves each row's ``entity`` + ``template``
    (``processRelationshipCollection`` → ``withConnectedData`` spreads the
    connection row's fields), so the from/to rows are identifiable.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rel in relations:
        if not isinstance(rel, dict) or rel.get("hub") is None:
            continue
        grouped.setdefault(str(rel["hub"]), []).append(rel)
    for rows in grouped.values():
        has_from = any(isinstance(r, dict) and str(r.get("entity")) == str(new_from) and not r.get("template") for r in rows)
        has_to = any(
            isinstance(r, dict) and str(r.get("entity")) == str(new_to) and str(r.get("template")) == str(relation_type)
            for r in rows
        )
        if has_from and has_to:
            return True
    return False


def build_relationship_gaps(
    expected_hubs: list[CapturedHub],
    id_map: dict[str, str],
    current_raws: dict[str, dict[str, Any] | None],
) -> list[RelationshipGap]:
    """Pure: compare captured mutual-deleted hubs against re-created entities' relations.

    For each captured hub, remap its endpoints via ``id_map`` (old→new) and
    check the re-created from-entity's ``relations`` for a matching hub. A hub
    whose endpoints were not both re-created (missing from ``id_map``) is skipped
    — the entity mismatch already flags the failed re-create. A re-created
    entity whose current raw is ``None`` is likewise skipped. Emits one
    :class:`RelationshipGap` per hub that did not come back. Pure: no I/O.
    """
    gaps: list[RelationshipGap] = []
    for hub in expected_hubs:
        new_from = id_map.get(hub.from_shared_id)
        new_to = id_map.get(hub.to_shared_id)
        if not new_from or not new_to:
            continue
        actual = current_raws.get(hub.from_shared_id)
        if not isinstance(actual, dict):
            continue
        relations_raw = actual.get("relations")
        relations = relations_raw if isinstance(relations_raw, list) else []
        if not _hub_exists(relations, new_from, new_to, hub.relation_type):
            gaps.append(
                RelationshipGap(
                    shared_id=hub.from_shared_id,
                    from_shared_id=new_from,
                    to_shared_id=new_to,
                    relation_type=hub.relation_type,
                )
            )
    return gaps


def build_inbound_ref_gaps(
    inbound_refs: list[InboundRef],
    id_map: dict[str, str],
    current_raws: dict[str, dict[str, Any] | None],
) -> list[RelationshipGap]:
    """Pure: check cascade-stripped inbound refs from still-existing entities were restored.

    For each :class:`InboundRef` (still-existing entity → deleted entity), remap
    the deleted endpoint via ``id_map`` (old→new) and check the still-existing
    entity's ``relations`` for a direction-matching hub ``existing → new``. A
    ref whose deleted target was not re-created (missing from ``id_map``) is
    skipped — the entity mismatch already flags the failed re-create. A still-
    existing entity whose current raw is ``None`` (unexpectedly absent) is
    skipped. Emits one :class:`RelationshipGap` per unrestored inbound ref
    (``shared_id`` = the still-existing entity). Pure: no I/O.
    """
    gaps: list[RelationshipGap] = []
    for ref in inbound_refs:
        new_to = id_map.get(ref.deleted_shared_id)
        if not new_to:
            continue
        actual = current_raws.get(ref.existing_shared_id)
        if not isinstance(actual, dict):
            continue
        relations_raw = actual.get("relations")
        relations = relations_raw if isinstance(relations_raw, list) else []
        if not _hub_exists(relations, ref.existing_shared_id, new_to, ref.relation_type):
            gaps.append(
                RelationshipGap(
                    shared_id=ref.existing_shared_id,
                    from_shared_id=ref.existing_shared_id,
                    to_shared_id=new_to,
                    relation_type=ref.relation_type,
                )
            )
    return gaps
