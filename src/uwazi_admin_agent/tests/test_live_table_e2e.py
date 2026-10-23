"""Live E2E: the ONE live review table survives a re-plan (no attempt slots).

The chat's Run button regenerates the run's script and re-plans it. The rows
the new script still produces keep their identity and status, changed rows go
back to pending, and rows the new script no longer produces become obsolete.

This exercises that against a live instance through the service layer (no UI).
"""

from __future__ import annotations

import asyncio

import pytest

from uwazi_admin_agent.configuration import RUNS_PATH
from uwazi_admin_agent.domain.suggestion import SuggestionFilter, SuggestionStatus
from uwazi_admin_agent.drivers.web.run_service import (
    build_suggestion_store,
    current_pass,
    run_auto_scan,
)
from uwazi_admin_agent.tests.e2e_support import (
    UWAZI_PASSWORD,
    UWAZI_URL,
    UWAZI_USER,
    AdminE2EWorkspace,
    update_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_a_replan_merges_into_the_same_live_table(workspace: AdminE2EWorkspace) -> None:
    """A second plan of the SAME run refreshes its rows instead of starting over."""
    ws = workspace
    run_id = ws.new_run(update_script(ws.template_name, list(ws.entity_ids), ws.languages.default, ws.value("first")))

    first = ws.suggestions(run_id)
    assert len(first) == len(ws.entity_ids)
    first_ids = {row.id for row in first}
    assert all(row.status is SuggestionStatus.PENDING for row in first)

    # Re-plan the SAME run with a different value: the script is replaced (as
    # the chat's regeneration does) and the pass runs again over the same run.
    script = update_script(ws.template_name, list(ws.entity_ids), ws.languages.default, ws.value("second"))
    (RUNS_PATH / run_id / "script.py").write_text(script, encoding="utf-8")
    asyncio.run(run_auto_scan(run_id, UWAZI_USER, UWAZI_PASSWORD))

    store = build_suggestion_store()
    rows, total = store.query(run_id, SuggestionFilter(include_obsolete=True, limit=100))
    assert total == len(ws.entity_ids)  # no duplicates, no second table
    # The rows kept their identity — the same row ids, not new ones.
    assert {row.id for row in rows} == first_ids
    # The changed proposal is there and the row is runnable again.
    assert all(row.proposed_value == ws.value("second") for row in rows)
    assert all(row.status is SuggestionStatus.PENDING for row in rows)

    # ONE pass row for the run.
    pass_row = current_pass(run_id)
    assert pass_row is not None
    assert pass_row.status.value == "completed"
    assert pass_row.entities_total == len(ws.entity_ids)


def test_a_row_the_new_script_drops_becomes_obsolete(workspace: AdminE2EWorkspace) -> None:
    """A row the regenerated script no longer produces is retired, not deleted."""
    ws = workspace
    keep = ws.entity_ids[0]
    drop = ws.entity_ids[1]
    run_id = ws.new_run(update_script(ws.template_name, [keep, drop], ws.languages.default, ws.value("first")))
    assert len(ws.suggestions(run_id)) == 2

    # The regenerated script covers only ONE of the two entities.
    script = update_script(ws.template_name, [keep], ws.languages.default, ws.value("second"))
    (RUNS_PATH / run_id / "script.py").write_text(script, encoding="utf-8")
    asyncio.run(run_auto_scan(run_id, UWAZI_USER, UWAZI_PASSWORD))

    store = build_suggestion_store()
    visible, visible_total = store.query(run_id, SuggestionFilter(limit=100))
    all_rows, all_total = store.query(run_id, SuggestionFilter(include_obsolete=True, limit=100))
    assert visible_total == 1
    assert all_total == 2
    assert visible[0].shared_id == keep
    dropped = [row for row in all_rows if row.shared_id == drop]
    assert dropped and dropped[0].status is SuggestionStatus.OBSOLETE
