"""Isolated unit tests for the per-property restore transform (pure)."""

from __future__ import annotations

from datetime import datetime, timezone

from uwazi_admin_agent.domain.property_restore import (
    property_restore_payload,
    row_snapshot_for_language,
    snapshot_row_raw,
)
from uwazi_admin_agent.domain.snapshot import EntityRowSnapshot, EntitySnapshot
from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY


def _snapshot(*rows: EntityRowSnapshot, language: str | None = None, raw: dict | None = None) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id="entity-a",
        language=language,
        raw=raw or {},
        captured_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        rows=list(rows),
    )


def test_restores_one_metadata_key_and_keeps_every_other_live_value() -> None:
    live = {"shared_id": "entity-a", "metadata": {"a": [{"value": "new a"}], "b": [{"value": "live b"}]}}
    backup = {"shared_id": "entity-a", "metadata": {"a": [{"value": "old a"}], "b": [{"value": "old b"}]}}

    payload = property_restore_payload(live, backup, "a")

    assert payload["metadata"]["a"] == [{"value": "old a"}]
    # The run's other write to this entity survives the single-property undo.
    assert payload["metadata"]["b"] == [{"value": "live b"}]
    # The input is not mutated.
    assert live["metadata"]["a"] == [{"value": "new a"}]


def test_a_property_absent_from_the_backup_is_removed_not_emptied() -> None:
    live = {"metadata": {"added_by_the_run": [{"value": "x"}], "kept": [{"value": "y"}]}}
    backup = {"metadata": {"kept": [{"value": "old"}]}}

    payload = property_restore_payload(live, backup, "added_by_the_run")

    assert "added_by_the_run" not in payload["metadata"]
    assert payload["metadata"]["kept"] == [{"value": "y"}]


def test_title_restores_from_the_top_level_raw() -> None:
    live = {"title": "New title", "metadata": {}}
    backup = {"title": "Old title", "metadata": {}}

    assert property_restore_payload(live, backup, TITLE_PROPERTY)["title"] == "Old title"
    assert property_restore_payload({"metadata": {}}, {"metadata": {}}, TITLE_PROPERTY).get("title") is None


def test_snapshot_row_raw_picks_the_language_row() -> None:
    snapshot = _snapshot(
        EntityRowSnapshot(language="en", raw={"title": "English"}),
        EntityRowSnapshot(language="es", raw={"title": "Español"}),
    )
    assert snapshot_row_raw(snapshot, "es") == {"title": "Español"}
    assert snapshot_row_raw(snapshot, "en") == {"title": "English"}
    assert snapshot_row_raw(snapshot, "fr") is None
    assert row_snapshot_for_language(snapshot, "es").language == "es"
    assert row_snapshot_for_language(snapshot, "fr") is None


def test_legacy_snapshot_stands_in_for_its_own_language_only() -> None:
    legacy = _snapshot(language="es", raw={"title": "Legacy"})
    assert snapshot_row_raw(legacy, "es") == {"title": "Legacy"}
    assert snapshot_row_raw(legacy, "en") is None
