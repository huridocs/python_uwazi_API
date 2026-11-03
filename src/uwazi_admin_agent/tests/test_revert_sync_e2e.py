"""Live E2E: a reverted re-creating operation becomes runnable again on sync.

These tests exercise the real service layer against a local Uwazi instance:

1. a merge run updates its target and deletes its source;
2. the run-level Revert restores the target and re-creates the source with a
   NEW sharedId;
3. "Sync with Uwazi" re-runs the script, which discovers the re-created source
   by title, so reconciliation puts the merge row back to PENDING with the live
   source id;
4. the merge can be executed again.

The localhost guard refuses any non-loopback target before a client is built.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.suggestion import SuggestionStatus
from uwazi_admin_agent.tests.e2e_support import (
    DEFAULT_PROPERTY,
    UWAZI_URL,
    AdminE2EWorkspace,
    delete_template_script,
    merge_entities_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_sync_returns_a_reverted_merge_to_pending_and_it_runs_again(workspace: AdminE2EWorkspace) -> None:
    ws = workspace
    target_id, source_id = ws.entity_ids[:2]
    language = ws.languages.default
    target_title = ws.raw(target_id, language)["title"]
    source_title = ws.raw(source_id, language)["title"]
    merged_value = ws.value("merged")
    run_id = ws.new_run(
        merge_entities_script(
            ws.template_name,
            target_title,
            source_title,
            language,
            merged_value,
        )
    )

    planned = ws.suggestions(run_id)
    merge_rows = [row for row in planned if row.op == "update_and_delete"]
    assert [row.shared_id for row in merge_rows] == [target_id]
    assert merge_rows[0].merge_source_ids == [source_id]

    ws.apply(run_id)
    assert ws.status(run_id) == RunStatus.EXECUTED
    assert ws.text(target_id, language) == merged_value
    assert ws.raw(source_id, language) is None

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    restored_id = ws.manifest(run_id).deleted[0].restored_shared_id
    assert restored_id is not None
    assert restored_id != source_id
    restored_raw = ws.raw(restored_id, language)
    assert restored_raw is not None
    assert restored_raw["title"] == source_title

    # The re-created entity must be searchable before the planning pass can
    # discover it by title.
    ws.wait_for_searchable([target_id, restored_id])

    reverted_rows = ws.suggestions(run_id)
    reverted_merge = next(row for row in reverted_rows if row.op == "update_and_delete")
    assert reverted_merge.status is SuggestionStatus.REVERTED
    assert reverted_merge.merge_source_ids == [source_id]

    result = ws.update_table(run_id)
    assert result.reconciliation.status_reset >= 1
    assert result.reconciliation.obsolete == 0

    synced_rows = ws.suggestions(run_id)
    synced_merge = next(row for row in synced_rows if row.op == "update_and_delete")
    assert synced_merge.id == reverted_merge.id
    assert synced_merge.status is SuggestionStatus.PENDING
    assert synced_merge.merge_source_ids == [restored_id]
    assert synced_merge.shared_id == target_id
    assert synced_merge.property_name == DEFAULT_PROPERTY

    # And the row really is executable again against the re-created source.
    ws.apply(run_id)
    assert ws.status(run_id) == RunStatus.EXECUTED
    assert ws.text(target_id, language) == merged_value
    assert ws.raw(restored_id, language) is None


def test_sync_makes_a_reverted_delete_runnable_under_the_new_shared_id(workspace: AdminE2EWorkspace) -> None:
    """A delete's revert re-creates the entity; sync retires the old row and
    inserts the new one as PENDING, so the operation can run again."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    title = ws.raw(shared_id, language)["title"]
    run_id = ws.new_run(delete_template_script(ws.template_name, title, language))

    planned = ws.suggestions(run_id)
    assert [(row.shared_id, row.op) for row in planned] == [(shared_id, "delete")]

    ws.apply(run_id)
    assert ws.status(run_id) == RunStatus.EXECUTED
    assert ws.raw(shared_id, language) is None

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    restored_id = ws.manifest(run_id).deleted[0].restored_shared_id
    assert restored_id is not None
    assert restored_id != shared_id
    assert ws.raw(restored_id, language)["title"] == title
    ws.wait_for_searchable([restored_id])

    result = ws.update_table(run_id)
    assert result.reconciliation.inserted >= 1
    assert result.reconciliation.obsolete >= 1

    rows = ws.suggestions(run_id, include_obsolete=True)
    by_id = {row.shared_id: row for row in rows}
    assert by_id[shared_id].status is SuggestionStatus.OBSOLETE
    assert by_id[restored_id].status is SuggestionStatus.PENDING
    assert by_id[restored_id].op == "delete"

    # The visible, runnable row is the new one — and it can delete the
    # re-created entity.
    ws.apply(run_id)
    assert ws.status(run_id) == RunStatus.EXECUTED
    assert ws.raw(restored_id, language) is None
