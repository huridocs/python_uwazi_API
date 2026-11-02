"""Live E2E: the per-instance on-disk layout really isolates two instances.

The admin agent can administer several Uwazi instances at once. This test
exercises the load-bearing part of that: a run and a task created for ONE
instance land under that instance's own tree, and the OTHER instance's tree
stays empty. Nothing here touches a second live instance — the second
namespace is a URL that is not the configured one, so its tree is provably
untouched.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.configuration import INSTANCE_IDS_DIR
from uwazi_admin_agent.domain.instance import instance_slug
from uwazi_admin_agent.domain.instance_paths import instance_paths
from uwazi_admin_agent.drivers.web.run_service import (
    build_suggestion_store,
    build_task_store,
    list_runs,
)
from uwazi_admin_agent.tests.e2e_support import (
    UWAZI_URL,
    AdminE2EWorkspace,
    update_script,
)
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e

# A namespace that is NOT the configured instance: its tree must stay empty.
OTHER_URL = "http://other-instance.invalid:3000"


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def test_a_run_lives_under_its_own_instance_tree(workspace: AdminE2EWorkspace) -> None:
    """The seeded run's folder is under the INSTANCE's runs dir, nowhere else."""
    ws = workspace
    run_id = ws.next_run_id("instance")
    script = update_script(ws.template_name, [ws.entity_ids[0]], ws.languages.default, ws.value("instance"))

    ws.seed_run(run_id, script)

    paths = instance_paths(UWAZI_URL, INSTANCE_IDS_DIR)
    assert (paths.runs_path / run_id).is_dir()
    assert (paths.runs_path / run_id / "script.py").is_file()


def test_the_suggestion_db_is_the_instances_own_file(workspace: AdminE2EWorkspace) -> None:
    """The review rows live in the INSTANCE's own suggestions database."""
    ws = workspace
    run_id = ws.next_run_id("instance")
    script = update_script(ws.template_name, [ws.entity_ids[0]], ws.languages.default, ws.value("instance"))

    ws.seed_run(run_id, script)

    paths = instance_paths(UWAZI_URL, INSTANCE_IDS_DIR)
    assert paths.suggestions_db.is_file()
    assert build_suggestion_store(UWAZI_URL).get_pass(run_id) is not None


def test_another_instances_tree_stays_empty(workspace: AdminE2EWorkspace) -> None:
    """A run created for one instance is INVISIBLE to another instance."""
    ws = workspace
    run_id = ws.next_run_id("instance")
    script = update_script(ws.template_name, [ws.entity_ids[0]], ws.languages.default, ws.value("instance"))

    ws.seed_run(run_id, script)

    other = instance_paths(OTHER_URL, INSTANCE_IDS_DIR)
    assert other.root != instance_paths(UWAZI_URL, INSTANCE_IDS_DIR).root
    assert not (other.runs_path / run_id).exists()
    assert list_runs(OTHER_URL) == []


def test_a_task_lives_under_its_own_instance_tree(workspace: AdminE2EWorkspace) -> None:
    """The task's YAML is under the INSTANCE's tasks dir, nowhere else."""
    ws = workspace
    run_id = ws.next_run_id("instance")
    task_id = ws.next_task_id("instance")
    script = update_script(ws.template_name, [ws.entity_ids[0]], ws.languages.default, ws.value("instance"))

    ws.create_task(
        task_id,
        "instance task",
        [(run_id, "update the default row", "deterministic test prompt")],
        {run_id: script},
    )

    paths = instance_paths(UWAZI_URL, INSTANCE_IDS_DIR)
    assert (paths.tasks_path / f"{task_id}.yaml").is_file()
    assert build_task_store(UWAZI_URL).load_task(task_id).task_id == task_id

    other = instance_paths(OTHER_URL, INSTANCE_IDS_DIR)
    assert not (other.tasks_path / f"{task_id}.yaml").exists()


def test_the_instance_slug_names_the_directory(workspace: AdminE2EWorkspace) -> None:
    """The instance's directory is the slug of its URL."""
    paths = instance_paths(UWAZI_URL, INSTANCE_IDS_DIR)

    assert paths.root.name == instance_slug(UWAZI_URL)
