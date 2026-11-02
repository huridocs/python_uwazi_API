"""Isolated unit tests for verifying a PROPERTY-SCOPED revert (pure).

A property-scoped snapshot holds a minimal raw — identity, title and the touched
keys only — so verification must compare exactly that, not the whole entity.
"""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.revert_verification import property_row_differs, verify_revert
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot


def _row(touched: list[str], metadata: dict | None = None) -> EntityRowSnapshot:
    return EntityRowSnapshot(
        language="en",
        internal_id="internal-1",
        raw={
            "_id": "internal-1",
            "sharedId": "A",
            "language": "en",
            "title": "Old title",
            "metadata": metadata or {},
        },
        touched_properties=touched,
    )


def _snapshot(row: EntityRowSnapshot) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id="A",
        internal_id="internal-1",
        language="en",
        raw=row.raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        scope="property",
        rows=[row],
    )


def _manifest() -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        modified=[EntityIdentity(shared_id="A", language="en")],
        status=RunStatus.REVERTED,
    )


def test_matching_touched_values_and_title_differ_not() -> None:
    row = _row(["a"], {"a": [{"value": "old"}]})
    current = {
        "metadata": {"a": [{"value": "old"}], "untouched": [{"value": "live"}]},
        "title": "Old title",
    }

    assert property_row_differs(row, current) is False


def test_a_different_touched_value_is_a_mismatch() -> None:
    row = _row(["a"], {"a": [{"value": "old"}]})

    assert property_row_differs(row, {"metadata": {"a": [{"value": "still new"}]}, "title": "Old title"}) is True


def test_a_property_the_run_created_must_be_gone() -> None:
    row = _row(["brand_new"])

    assert property_row_differs(row, {"metadata": {"brand_new": [{"value": "x"}]}, "title": "Old title"}) is True
    assert property_row_differs(row, {"metadata": {}, "title": "Old title"}) is False


def test_a_changed_title_is_a_mismatch() -> None:
    row = _row(["a"], {"a": [{"value": "old"}]})

    assert property_row_differs(row, {"metadata": {"a": [{"value": "old"}]}, "title": "New title"}) is True


def test_an_absent_entity_is_a_mismatch() -> None:
    assert property_row_differs(_row(["a"]), None) is True


def test_verify_revert_ignores_properties_the_run_never_touched() -> None:
    row = _row(["a"], {"a": [{"value": "old"}]})
    current = {
        "metadata": {
            "a": [{"value": "old"}],
            # Written by somebody else after the run: not this snapshot's business.
            "someone_else": [{"value": "changed"}],
        },
        "title": "Old title",
    }

    result = verify_revert(_manifest(), {"A": _snapshot(row)}, {"A": current})

    assert result.mismatches == []
    assert result.ok is True


def test_verify_revert_flags_a_touched_property_that_stayed_changed() -> None:
    row = _row(["a"], {"a": [{"value": "old"}]})
    current = {"metadata": {"a": [{"value": "still new"}]}, "title": "Old title"}

    result = verify_revert(_manifest(), {"A": _snapshot(row)}, {"A": current})

    assert [mismatch.kind for mismatch in result.mismatches] == ["entity"]
    assert result.ok is False
