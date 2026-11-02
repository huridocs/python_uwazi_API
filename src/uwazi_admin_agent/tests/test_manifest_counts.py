"""Isolated unit tests for the run-manifest touch counts (per AGENTS.md).

The runs table's Modified/Deleted/Created/Rewired figures are derived here: a
full revert zeroes them, a partial (per-entity or per-property) revert subtracts
the restored entities while keeping the touch-set lists for audit/verification,
and a later write makes the entity outstanding again.

No mocks, no real instance: literal manifests and plain assertions.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.manifest import (
    MigrationManifest,
    RewiredRelationship,
    RunStatus,
)
from uwazi_admin_agent.domain.snapshot import EntityIdentity

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _manifest(
    *,
    status: RunStatus = RunStatus.EXECUTED,
    modified: list[str] | None = None,
    deleted: list[str] | None = None,
    created: list[str] | None = None,
    rewired: list[str] | None = None,
    reverted: list[str] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=NOW,
        prompt="p",
        script="s",
        status=status,
        modified=[EntityIdentity(shared_id=sid) for sid in (modified or [])],
        deleted=[EntityIdentity(shared_id=sid) for sid in (deleted or [])],
        created=[EntityIdentity(shared_id=sid) for sid in (created or [])],
        rewired=[
            RewiredRelationship(entity=EntityIdentity(shared_id=sid), property_name="relations", before=[])
            for sid in (rewired or [])
        ],
        reverted_shared_ids=list(reverted or []),
    )


def test_an_executed_run_counts_its_whole_touch_set() -> None:
    manifest = _manifest(modified=["A", "B"], deleted=["C"], created=["D"], rewired=["E"])

    assert manifest.touch_counts() == (2, 1, 1, 1)


def test_a_fully_reverted_run_counts_zero() -> None:
    """A full revert keeps the manifest history but there is nothing live."""
    manifest = _manifest(
        status=RunStatus.REVERTED,
        modified=["A", "B"],
        deleted=["C"],
        created=["D"],
        rewired=["E"],
    )

    assert manifest.touch_counts() == (0, 0, 0, 0)


def test_a_partial_revert_subtracts_only_the_restored_entities() -> None:
    """The reported bug: reverting one row left Modified at the historical total."""
    manifest = _manifest(
        modified=["A", "B"],
        deleted=["C", "D"],
        created=["E"],
        rewired=["A", "C", "F"],
        reverted=["A", "C"],
    )

    assert manifest.touch_counts() == (1, 1, 1, 1)


def test_a_suboperation_revert_keeps_the_entity_counted_while_siblings_remain() -> None:
    """Only a whole entity leaves the count; a single property undo must not."""
    manifest = _manifest(modified=["A"], reverted=[])

    assert manifest.touch_counts() == (1, 0, 0, 0)


def test_mark_reverted_is_idempotent_and_keeps_first_appearance_order() -> None:
    manifest = _manifest(modified=["A", "B"])

    manifest.mark_reverted(["B", "B", ""])
    manifest.mark_reverted(["A"])

    assert manifest.reverted_shared_ids == ["B", "A"]
    assert manifest.touch_counts() == (0, 0, 0, 0)


def test_mark_written_makes_a_reverted_entity_outstanding_again() -> None:
    """Re-running a restored row after a partial revert counts it once more."""
    manifest = _manifest(modified=["A", "B"], reverted=["A"])

    manifest.mark_written(["A"])

    assert manifest.reverted_shared_ids == []
    assert manifest.touch_counts() == (2, 0, 0, 0)


def test_mark_written_ignores_unknown_and_empty_ids() -> None:
    manifest = _manifest(modified=["A"], reverted=["A"])

    manifest.mark_written([])
    manifest.mark_written(["Z"])

    assert manifest.reverted_shared_ids == ["A"]


def test_reset_touch_set_clears_the_partial_revert_markers() -> None:
    """A re-execute starts a new execution, so old revert markers must not leak."""
    manifest = _manifest(modified=["A"], reverted=["A"])
    manifest.applied_writes = []
    manifest.result = "done"

    manifest.reset_touch_set()

    assert manifest.reverted_shared_ids == []
    assert manifest.touch_counts() == (0, 0, 0, 0)
