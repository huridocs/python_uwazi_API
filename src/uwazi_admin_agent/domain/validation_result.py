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


def build_validation_outcome(
    script_result: str | None,
    script_error: str | None,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any] | None],
    post_revert: dict[str, dict[str, Any] | None],
    created_shared_ids: list[str] | None = None,
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
        if actual != expected:
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
    )
