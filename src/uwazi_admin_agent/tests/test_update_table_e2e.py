"""Live E2E: "Update table from Uwazi" through the service layer (no UI)."""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus
from uwazi_admin_agent.tests.e2e_support import (
    DEFAULT_PROPERTY,
    UWAZI_URL,
    AdminE2EWorkspace,
    update_script,
    update_template_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def _row_snapshot(rows: list[Suggestion]) -> dict[int, tuple[object, object, object]]:
    return {row.id: (row.current_value, row.proposed_value, row.status) for row in rows}


def test_refresh_without_external_changes_keeps_the_table(workspace: AdminE2EWorkspace) -> None:
    """A no-op refresh adds nothing, changes nothing, and records its timestamp."""
    ws = workspace
    run_id = ws.new_run(update_template_script(ws.template_name, f"{ws.namespace}_", ws.languages.default, ws.value("noop")))
    before = _row_snapshot(ws.suggestions(run_id))
    assert len(before) == len(ws.entity_ids)

    result = ws.update_table(run_id)
    assert result.changed_in_uwazi == 0
    assert result.reconciliation.inserted == 0
    assert result.reconciliation.refreshed == 0
    assert result.reconciliation.status_reset == 0
    assert result.reconciliation.reappeared == 0
    assert result.reconciliation.obsolete == 0
    assert result.reconciliation.kept_applied == 0
    assert ws.table_update_time(run_id) is not None

    after = _row_snapshot(ws.suggestions(run_id))
    assert after == before
    assert all(row.status == SuggestionStatus.PENDING for row in ws.suggestions(run_id))


def test_external_change_blocks_preflight_until_refresh(workspace: AdminE2EWorkspace) -> None:
    """A human edit is detected, the stale selection is blocked, and a refresh unblocks it."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    run_id = ws.new_run(update_script(ws.template_name, [shared_id], language, ws.value("stale")))
    stale_rows = ws.suggestions(run_id)
    assert len(stale_rows) == 1
    assert stale_rows[0].current_value

    ws.external_update(shared_id, ws.value("external"), language)
    blocked = ws.preflight(run_id, stale_rows)
    assert not blocked.ok
    assert [(issue.shared_id, issue.kind) for issue in blocked.issues] == [(shared_id, "changed")]

    result = ws.update_table(run_id)
    assert result.changed_in_uwazi is not None
    assert result.changed_in_uwazi >= 1
    assert result.reconciliation.refreshed >= 1
    assert result.reconciliation.inserted == 0
    assert result.reconciliation.obsolete == 0

    refreshed_rows = ws.suggestions(run_id)
    assert len(refreshed_rows) == 1
    refreshed = refreshed_rows[0]
    assert refreshed.shared_id == shared_id
    assert refreshed.language == language
    assert refreshed.match is False
    assert ws.text(shared_id, language) == ws.value("external")
    assert ws.table_update_time(run_id) is not None

    after = ws.preflight(run_id, refreshed_rows)
    assert after.ok, [issue.describe() for issue in after.issues]


def test_external_delete_marks_the_row_obsolete(workspace: AdminE2EWorkspace) -> None:
    """An entity deleted in Uwazi disappears from the runnable table as an obsolete row."""
    ws = workspace
    deleted_id = ws.entity_ids[0]
    run_id = ws.new_run(
        update_template_script(ws.template_name, f"{ws.namespace}_", ws.languages.default, ws.value("delete"))
    )
    assert len(ws.suggestions(run_id)) == len(ws.entity_ids)

    ws.external_delete(deleted_id)
    result = ws.update_table(run_id)
    assert result.reconciliation.obsolete >= 1
    assert result.changed_in_uwazi is not None

    visible = ws.suggestions(run_id)
    assert deleted_id not in {row.shared_id for row in visible}
    assert len(visible) == len(ws.entity_ids) - 1

    all_rows = ws.suggestions(run_id, include_obsolete=True)
    obsolete = [row for row in all_rows if row.shared_id == deleted_id]
    assert len(obsolete) == 1
    assert obsolete[0].status == SuggestionStatus.OBSOLETE


def test_external_new_entity_is_inserted(workspace: AdminE2EWorkspace) -> None:
    """An entity created in Uwazi gets a new review row on the next refresh."""
    ws = workspace
    run_id = ws.new_run(
        update_template_script(ws.template_name, f"{ws.namespace}_", ws.languages.default, ws.value("insert"))
    )
    before_ids = {row.shared_id for row in ws.suggestions(run_id)}
    assert len(before_ids) == len(ws.entity_ids)

    new_id = ws.external_create("insert")
    result = ws.update_table(run_id)
    assert result.reconciliation.inserted >= 1
    assert result.changed_in_uwazi is not None

    rows = ws.suggestions(run_id)
    by_id = {row.shared_id: row for row in rows}
    assert new_id in by_id
    assert by_id[new_id].language == ws.languages.default
    assert by_id[new_id].property_name == DEFAULT_PROPERTY
    assert by_id[new_id].proposed_value == ws.value("insert")
    assert len(by_id) == len(before_ids) + 1


def test_double_refresh_is_idempotent(workspace: AdminE2EWorkspace) -> None:
    """Refreshing twice keeps the same row ids and changes nothing the second time."""
    ws = workspace
    run_id = ws.new_run(
        update_template_script(ws.template_name, f"{ws.namespace}_", ws.languages.default, ws.value("double"))
    )
    first_ids = {row.id for row in ws.suggestions(run_id)}
    assert first_ids

    first = ws.update_table(run_id)
    assert first.reconciliation.inserted == 0
    assert first.reconciliation.refreshed == 0
    assert first.reconciliation.obsolete == 0
    after_first = _row_snapshot(ws.suggestions(run_id))

    second = ws.update_table(run_id)
    assert second.changed_in_uwazi == 0
    assert second.reconciliation.inserted == 0
    assert second.reconciliation.refreshed == 0
    assert second.reconciliation.status_reset == 0
    assert second.reconciliation.reappeared == 0
    assert second.reconciliation.obsolete == 0
    assert second.reconciliation.kept_applied == 0

    after_second = _row_snapshot(ws.suggestions(run_id))
    assert {row.id for row in ws.suggestions(run_id)} == first_ids
    assert after_second == after_first
