"""Live E2E: language rows are discovered, isolated, and reverted per row."""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.task import TASK_REVERTED, TASK_RUNNING
from uwazi_admin_agent.tests.e2e_support import (
    DEFAULT_PROPERTY,
    UWAZI_URL,
    AdminE2EWorkspace,
    update_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_languages_are_discovered_and_every_row_exists(workspace: AdminE2EWorkspace) -> None:
    """The fixture follows the instance's configured languages, not hardcoded ones."""
    ws = workspace
    assert ws.languages.keys
    assert ws.languages.default in ws.languages.keys
    assert ws.languages.keys.count(ws.languages.default) == 1

    for shared_id in ws.entity_ids:
        for language in ws.languages.keys:
            raw = ws.raw(shared_id, language)
            assert raw is not None, f"missing {language!r} row for {shared_id}"
            assert raw.get("language") == language


def test_single_run_updates_only_the_requested_language(workspace: AdminE2EWorkspace) -> None:
    """A write to one language row leaves its sibling row byte-identical."""
    ws = workspace
    secondary = ws.require_secondary()
    shared_id = ws.entity_ids[0]
    default = ws.languages.default
    before = ws.baseline([(shared_id, default), (shared_id, secondary)])
    run_id = ws.new_run(update_script(ws.template_name, [shared_id], secondary, ws.value("only-secondary")))

    rows = ws.suggestions(run_id)
    assert len(rows) == 1
    assert rows[0].shared_id == shared_id
    assert rows[0].language == secondary
    assert rows[0].property_name == DEFAULT_PROPERTY

    ws.apply(run_id)
    assert ws.text(shared_id, secondary) == ws.value("only-secondary")
    assert ws.text(shared_id, default) == "original-0"
    assert ws.text(shared_id, secondary) != ws.text(shared_id, default)

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    ws.assert_restored(before)


def test_two_step_task_writes_one_language_per_step_then_reverts(workspace: AdminE2EWorkspace) -> None:
    """The multistep-per-language task shape keeps each row independent and reverts both."""
    ws = workspace
    secondary = ws.require_secondary()
    shared_id = ws.entity_ids[0]
    default = ws.languages.default
    before = ws.baseline([(shared_id, default), (shared_id, secondary)])

    default_run = ws.next_run_id("default-language")
    secondary_run = ws.next_run_id("secondary-language")
    task_id = ws.next_task_id("per-language")
    ws.create_task(
        task_id,
        "one language per step",
        [
            (default_run, f"update the {default} row", "deterministic default-language step"),
            (secondary_run, f"update the {secondary} row", "deterministic secondary-language step"),
        ],
        {
            default_run: update_script(ws.template_name, [shared_id], default, ws.value("task-default")),
            secondary_run: update_script(ws.template_name, [shared_id], secondary, ws.value("task-secondary")),
        },
    )

    ws.apply(default_run)
    assert ws.text(shared_id, default) == ws.value("task-default")
    assert ws.text(shared_id, secondary) == "original-0"
    assert ws.status(default_run) == RunStatus.EXECUTED

    ws.apply(secondary_run)
    assert ws.text(shared_id, default) == ws.value("task-default")
    assert ws.text(shared_id, secondary) == ws.value("task-secondary")
    assert ws.text(shared_id, default) != ws.text(shared_id, secondary)

    view = ws.task_view(task_id)
    assert view.status == TASK_RUNNING
    assert view.revert_targets == [("step", secondary_run), ("step", default_run)]

    reverted = ws.revert_task(task_id)
    assert reverted == [secondary_run, default_run]
    assert ws.status(default_run) == RunStatus.REVERTED
    assert ws.status(secondary_run) == RunStatus.REVERTED
    ws.assert_restored(before)
    assert ws.task_view(task_id).status == TASK_REVERTED
