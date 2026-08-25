"""The validation-result shape for the dummy-entity gate (§2.7, Phase 3).

A candidate script is run against throwaway dummy entities in the real instance.
The harness records, per original dummy, the exact raw before-state, the
post-script after-state, and the post-revert state. This module holds the
**pure** pieces the gate is built from: the result/diff/mismatch models and
:func:`build_validation_outcome`, which assembles a :class:`ValidationResult`
from literal raw dicts. These are the unit-test target named by the Phase 3 DoD
("validation-result shape, the exact-raw-restore comparison, the diff"); the
live harness that gathers the dicts from Uwazi lives in
``use_cases/dummy_entity_harness.py`` and is validated via the simulation run.

``passed`` means the script ran cleanly (no exception, ``result`` set) **and**
every *original* dummy's post-revert raw equals its before raw — i.e. the
script's changes are exactly reversible. Semantic correctness ("did it do the
right thing?") is not something the harness can judge; the per-dummy
before/after diff and the script's ``result`` string are returned to the LLM so
it can judge that and repair. Script-created dummies (no before-state) are
tracked for cleanup only and are excluded from the restore-equality check
(created/deleted revert categories land in Phase 4).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Fields Uwazi bumps server-side on every save (confirmed by live probe: a no-op
# save of an identical raw advances ``editDate`` by a few milliseconds, ignoring
# the posted value). These can NEVER be restored to their before-value by re-posting
# the raw — the save itself bumps them — so the restore-equality check excludes
# them. Backup/restore still preserves them in the snapshot (raw fidelity, §2.5);
# only the *comparison* ignores this set. Add a field here only when a live probe
# proves Uwazi mutates it on save.
PLATFORM_MANAGED_FIELDS: frozenset[str] = frozenset({"editDate"})

# Identity fields re-minted by Uwazi when an entity is **re-created** via the
# create branch (delete-revert). They differ by design between the snapshot and
# the re-created entity, so the deleted-entry verification excludes them. They
# are NOT added to PLATFORM_MANAGED_FIELDS — modified entities must keep the
# same _id/sharedId, so identity is only ignored for the re-create case.
IDENTITY_FIELDS: frozenset[str] = frozenset({"_id", "sharedId"})

# File-bearing fields on a raw entity that are **denormalized views** of the
# `files` collection (joined by `entity=<sharedId>` and split by `type` — see
# `app/api/entities/entities.js::withDocuments`). On delete-revert the entity is
# re-created with a fresh sharedId and its files are re-uploaded, minting fresh
# file _ids/filenames, so the `documents`/`attachments` arrays can never match
# the snapshot's by identity. The deleted-entry raw-diff excludes these fields
# (file identity is inherently not preserved); a dedicated file-verification
# check in `domain/revert_verification.py` compares by originalname+kind+
# language to detect actual file-restore gaps (missing/extra files). Modified
# entities keep their files (the update branch keeps _id/sharedId), so these are
# only ignored for the re-create case, like IDENTITY_FIELDS.
FILE_FIELDS: frozenset[str] = frozenset({"documents", "attachments"})

# Fields excluded from the restore comparison **only** for an original the
# script DELETED and revert RE-CREATED (the create branch mints a fresh
# ``_id``/``sharedId`` and re-uploaded files get fresh file ids). Mirrors the
# production ``revert_verification`` exclusion for deleted entries. Modified
# originals keep identity + files, so this set is used solely for the
# deleted-then-recreated case (``after[sid] is None`` and revert brought it
# back). A deleted original revert FAILED to bring back (``post_revert`` is
# None) falls through to the normal check -> ``None != expected`` -> mismatch.
_RECREATE_EXCLUDED_FIELDS: frozenset[str] = PLATFORM_MANAGED_FIELDS | IDENTITY_FIELDS | FILE_FIELDS


def _strip_platform_managed(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without :data:`PLATFORM_MANAGED_FIELDS` (``None`` passes through)."""
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if k not in PLATFORM_MANAGED_FIELDS}


def _strip_for_recreate(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return ``raw`` without :data:`_RECREATE_EXCLUDED_FIELDS` (``None`` passes through).

    Used only for the deleted-then-recreated original case: a re-created entity
    has a fresh ``_id``/``sharedId`` and re-uploaded files get fresh file ids, so
    those fields can never match the snapshot by identity. The DATA fields
    (title/template/metadata/url-attachments-as-data/...) are still compared, so
    a real data-loss revert (e.g. title not restored) is still caught.
    """
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if k not in _RECREATE_EXCLUDED_FIELDS}


class EntityDiff(BaseModel):
    """Per-dummy before/after comparison of the raw entity JSON.

    ``before`` is ``None`` for a dummy the script created (it had no
    pre-run state); ``after`` is ``None`` for a dummy the script deleted.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The dummy entity's sharedId.")
    before: dict[str, Any] | None = Field(description="Original raw, or None if the script created this dummy.")
    after: dict[str, Any] | None = Field(description="Post-script raw, or None if the script deleted this dummy.")

    @property
    def changed(self) -> bool:
        """True when before and after differ (including create/delete transitions)."""
        return self.before != self.after


class RestoreMismatch(BaseModel):
    """A dummy whose post-revert raw did not equal its original before raw."""

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The dummy entity's sharedId.")
    expected: dict[str, Any] = Field(description="The original raw captured before the script ran.")
    actual: dict[str, Any] | None = Field(
        description="The raw captured after revert, or None if revert failed to restore it."
    )


class ValidationResult(BaseModel):
    """The outcome of running a candidate script against the dummies (§2.7)."""

    passed: bool = Field(
        description="True iff the script ran cleanly AND every original dummy restored to its exact before raw."
    )
    script_result: str | None = Field(
        default=None, description="The `result` variable the script set, or None if it didn't set one."
    )
    script_error: str | None = Field(
        default=None,
        description="The script's error (type + message + traceback tail) if it raised; None otherwise.",
    )
    diffs: list[EntityDiff] = Field(default_factory=list, description="Per-dummy before/after diff (originals + created).")
    restore_equal: bool = Field(
        default=True, description="True iff every original dummy's post-revert raw equals its before raw."
    )
    restore_mismatches: list[RestoreMismatch] = Field(
        default_factory=list, description="Originals that did not restore exactly (empty when restore_equal)."
    )
    created_shared_ids: list[str] = Field(
        default_factory=list, description="sharedIds of dummies the script created (cleanup-tracked)."
    )
    cleanup_error: str | None = Field(
        default=None, description="Error raised while deleting the dummies, if cleanup failed (should not happen)."
    )
    es_settle_warning: str | None = Field(
        default=None,
        description=(
            "Warning recorded when the ES-visibility settle timed out before every "
            "freshly-created dummy was indexed (Option A). The gate proceeds best-effort "
            "and deletes anyway; the shared index MAY be inconsistent and need a reindex. "
            "Set by the harness, not the pure builder."
        ),
    )


def build_validation_outcome(
    script_result: str | None,
    script_error: str | None,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any] | None],
    post_revert: dict[str, dict[str, Any] | None],
    created_shared_ids: list[str] | None = None,
    es_settle_warning: str | None = None,
) -> ValidationResult:
    """Assemble a :class:`ValidationResult` from the gathered raw dicts.

    Pure: no I/O. ``before`` keys the *original* dummies (those the harness
    created before running the script). ``after`` keys every dummy seen
    post-script (originals + script-created); a ``None`` value means the script
    deleted it. ``post_revert`` keys the originals after the restore step; a
    ``None`` value means revert failed to bring it back.

    Restore-equality is checked **only** for originals (keys of ``before``);
    script-created dummies (``created_shared_ids``) have no before-state to
    restore to and are recorded for cleanup only.
    """
    diffs: list[EntityDiff] = [
        EntityDiff(shared_id=sid, before=before.get(sid), after=after_raw) for sid, after_raw in after.items()
    ]

    mismatches: list[RestoreMismatch] = []
    for sid, expected in before.items():
        actual = post_revert.get(sid)
        # Compare data fields only. Two cases:
        # 1. Deleted-then-recreated original (script deleted it AND revert
        #    re-created it via the create branch with a fresh sharedId): compare
        #    DATA only, excluding identity + file-bearing fields (re-minted on
        #    re-create) on top of the always-excluded platform-managed fields.
        #    Mirrors production ``revert_verification`` for deleted entries.
        # 2. Everything else (modified, or a deleted original revert failed to
        #    bring back -> ``actual`` is None): compare excluding platform-managed
        #    fields only. ``None != expected`` correctly records a mismatch when
        #    revert failed to restore a deleted original.
        # The recorded mismatch still carries the FULL expected/actual raws so
        # the diagnostic output shows the real values (incl. identity/editDate)
        # when a real data field does differ.
        if after.get(sid) is None and actual is not None:
            expected_cmp = _strip_for_recreate(expected)
            actual_cmp = _strip_for_recreate(actual)
        else:
            expected_cmp = _strip_platform_managed(expected)
            actual_cmp = _strip_platform_managed(actual)
        if expected_cmp != actual_cmp:
            mismatches.append(RestoreMismatch(shared_id=sid, expected=expected, actual=actual))

    restore_equal = not mismatches
    ran_clean = script_error is None
    created = list(created_shared_ids) if created_shared_ids else []

    return ValidationResult(
        passed=ran_clean and restore_equal,
        script_result=script_result,
        script_error=script_error,
        diffs=diffs,
        restore_equal=restore_equal,
        restore_mismatches=mismatches,
        created_shared_ids=created,
        es_settle_warning=es_settle_warning,
    )
