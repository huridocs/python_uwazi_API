"""Live E2E: execute and revert flows through the service layer (no UI)."""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.revert_gate import RevertRefusedError
from uwazi_admin_agent.domain.task import TASK_REVERTED, TASK_RUNNING, StepOrderError
from uwazi_admin_agent.tests.e2e_support import (
    DEFAULT_PROPERTY,
    UWAZI_URL,
    AdminE2EWorkspace,
    create_entity_script,
    delete_entities_script,
    update_languages_script,
    update_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_single_run_revert_restores_every_language_row(workspace: AdminE2EWorkspace) -> None:
    """A run that writes every installed language is restored row for row."""
    ws = workspace
    shared_ids = list(ws.entity_ids)
    pairs = ws.pairs(shared_ids, ws.languages.keys)
    before = ws.baseline(pairs)
    values = {language: ws.value(f"all-{language}") for language in ws.languages.keys}
    run_id = ws.new_run(update_languages_script(ws.template_name, shared_ids, values))

    rows = ws.suggestions(run_id)
    assert {(row.shared_id, row.language) for row in rows} == set(pairs)
    assert {row.property_name for row in rows} == {DEFAULT_PROPERTY}

    ws.apply(run_id)
    assert ws.status(run_id) == RunStatus.EXECUTED
    for shared_id, language in pairs:
        ws.assert_property(shared_id, language, values[language])

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    ws.assert_restored(before)
    assert {row.status.value for row in ws.suggestions(run_id)} == {"reverted"}


def test_execute_revert_execute_revert_is_stable(workspace: AdminE2EWorkspace) -> None:
    """The working cycle can run twice and lands on the same baseline each time."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    pair = (shared_id, ws.languages.default)
    before = ws.baseline([pair])
    run_id = ws.new_run(update_script(ws.template_name, [shared_id], ws.languages.default, ws.value("cycle")))

    for _cycle in range(2):
        ws.apply(run_id)
        assert ws.status(run_id) == RunStatus.EXECUTED
        ws.assert_property(shared_id, ws.languages.default, ws.value("cycle"))

        ws.revert(run_id)
        assert ws.status(run_id) == RunStatus.REVERTED
        ws.assert_restored(before)
        assert {row.status.value for row in ws.suggestions(run_id)} == {"reverted"}


def test_whole_task_revert_walks_steps_in_reverse_order(workspace: AdminE2EWorkspace) -> None:
    """Two steps on one entity revert latest-first, restoring the pre-task state."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    before = ws.baseline([(shared_id, language)])

    first_run = ws.next_run_id("first")
    second_run = ws.next_run_id("second")
    task_id = ws.next_task_id("revert-order")
    ws.create_task(
        task_id,
        "two steps on one entity",
        [
            (first_run, "write the first value", "first deterministic step"),
            (second_run, "write the second value", "second deterministic step"),
        ],
        {
            first_run: update_script(ws.template_name, [shared_id], language, ws.value("one")),
            second_run: update_script(ws.template_name, [shared_id], language, ws.value("two")),
        },
    )

    ws.apply(first_run)
    ws.apply(second_run)
    assert ws.text(shared_id, language) == ws.value("two")
    view = ws.task_view(task_id)
    assert view.status == TASK_RUNNING
    assert view.revert_targets == [("step", second_run), ("step", first_run)]

    reverted = ws.revert_task(task_id)
    assert reverted == [second_run, first_run]
    assert ws.status(first_run) == RunStatus.REVERTED
    assert ws.status(second_run) == RunStatus.REVERTED
    ws.assert_restored(before)

    view = ws.task_view(task_id)
    assert view.status == TASK_REVERTED
    assert view.revert_targets == []
    assert [step.status for step in view.steps] == [RunStatus.REVERTED, RunStatus.REVERTED]


def test_reverting_a_created_entity_deletes_it(workspace: AdminE2EWorkspace) -> None:
    """A create row's undo is a deletion of the sharedId Uwazi minted."""
    ws = workspace
    title = ws.value("created")
    run_id = ws.new_run(
        create_entity_script(
            ws.template_name,
            title,
            ws.languages.default,
            {DEFAULT_PROPERTY: ws.value("created-metadata")},
        )
    )

    rows = ws.suggestions(run_id)
    assert {row.op for row in rows} == {"create"}
    ws.apply(run_id)

    manifest = ws.manifest(run_id)
    assert manifest.status == RunStatus.EXECUTED
    assert len(manifest.created) == 1
    created_id = manifest.created[0].shared_id
    created_raw = ws.raw(created_id, ws.languages.default)
    assert created_raw is not None
    assert created_raw["title"] == title
    assert ws.text(created_id, ws.languages.default) == ws.value("created-metadata")

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    for language in ws.languages.keys:
        assert ws.raw(created_id, language) is None, f"created entity still exists in {language}"


def test_reverting_a_deleted_entity_recreates_it_from_its_snapshot(workspace: AdminE2EWorkspace) -> None:
    """A delete's undo re-creates the entity with a fresh sharedId and the old content."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    before = ws.raw(shared_id, language)
    assert before is not None

    run_id = ws.new_run(delete_entities_script([shared_id]))
    rows = ws.suggestions(run_id)
    assert [(row.shared_id, row.op, row.property_name) for row in rows] == [(shared_id, "delete", None)]

    ws.apply(run_id)
    assert ws.raw(shared_id, language) is None
    manifest = ws.manifest(run_id)
    assert manifest.status == RunStatus.EXECUTED
    assert [entry.shared_id for entry in manifest.deleted] == [shared_id]

    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED
    restored_manifest = ws.manifest(run_id)
    restored_id = restored_manifest.deleted[0].restored_shared_id
    assert restored_id is not None
    assert restored_id != shared_id

    restored = ws.raw(restored_id, language)
    assert restored is not None
    assert restored["title"] == before["title"]
    assert restored["template"] == before["template"]
    assert restored["metadata"] == before["metadata"]
    assert ws.text(restored_id, language) == "original-0"


def test_reverting_one_selected_entity_leaves_its_sibling_applied(workspace: AdminE2EWorkspace) -> None:
    """The review table's per-entity revert restores only the checked entity."""
    ws = workspace
    first_id, second_id = ws.entity_ids[:2]
    language = ws.languages.default
    run_id = ws.new_run(update_script(ws.template_name, [first_id, second_id], language, ws.value("selection")))

    ws.apply(run_id)
    assert ws.text(first_id, language) == ws.value("selection")
    assert ws.text(second_id, language) == ws.value("selection")

    outcome = ws.revert_selection(run_id, [(first_id, language)])
    assert outcome.reverted == [first_id]
    assert outcome.failures == []
    assert outcome.skipped_created == []
    assert outcome.skipped_deleted == []

    assert ws.text(first_id, language) == "original-0"
    assert ws.text(second_id, language) == ws.value("selection")


def test_run_revert_refuses_a_run_that_was_already_reverted(workspace: AdminE2EWorkspace) -> None:
    """Re-reverting an already reverted run is refused to keep delete-reverts idempotent."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    run_id = ws.new_run(update_script(ws.template_name, [shared_id], language, ws.value("refusal")))

    ws.apply(run_id)
    ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED

    with pytest.raises(RevertRefusedError):
        ws.revert(run_id)
    assert ws.status(run_id) == RunStatus.REVERTED


def test_task_revert_refuses_a_task_with_no_executed_step(workspace: AdminE2EWorkspace) -> None:
    """Whole-task revert only has a target once a step's run has live writes."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    run_id = ws.next_run_id("planned")
    task_id = ws.next_task_id("planned")
    ws.create_task(
        task_id,
        "a task that has not run",
        [(run_id, "planned step", "deterministic planned step")],
        {run_id: update_script(ws.template_name, [shared_id], language, ws.value("planned"))},
    )

    with pytest.raises(StepOrderError):
        ws.revert_task(task_id)
    assert ws.status(run_id) == RunStatus.PLANNED
