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
  fresh _id/sharedId. The comparison excludes platform-managed **and** identity
  fields (:data:`IDENTITY_FIELDS`) — only DATA fields are expected to match.
  The current raw is fetched by the recorded ``restored_shared_id``; a ``None``
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
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.domain.validation_result import IDENTITY_FIELDS, PLATFORM_MANAGED_FIELDS

MismatchKind = Literal["entity", "relationship", "created"]


def _strip_platform_managed(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without :data:`PLATFORM_MANAGED_FIELDS` (``None`` passes through)."""
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if k not in PLATFORM_MANAGED_FIELDS}


def _strip_for_recreate(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without platform-managed and identity fields.

    For a deleted entity that was re-created via the create branch, Uwazi minted
    a fresh _id/sharedId, so those differ by design and must be excluded from the
    comparison (only the data fields are expected to match). Modified entities
    keep their identity, so they use the stricter _strip_platform_managed.
    """
    if raw is None:
        return None
    skip = PLATFORM_MANAGED_FIELDS | IDENTITY_FIELDS
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


class RevertVerificationResult(BaseModel):
    """Outcome of verifying a run's revert against its snapshots."""

    ok: bool = Field(description="True if every checked entity matched (no mismatches).")
    checked: int = Field(description="Number of entities checked.")
    mismatches: list[VerificationMismatch] = Field(
        default_factory=list, description="Per-entity mismatches (empty when ok)."
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
    """
    mismatches: list[VerificationMismatch] = []
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
        # are fresh by design - compare DATA fields only (excl. platform-managed and
        # identity). A None actual means the re-create failed (the old id is gone).
        if _strip_for_recreate(actual) != _strip_for_recreate(snap.raw):
            mismatches.append(
                VerificationMismatch(shared_id=deleted.shared_id, kind="entity", expected=snap.raw, actual=actual)
            )

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

    return RevertVerificationResult(ok=not mismatches, checked=checked, mismatches=mismatches)
