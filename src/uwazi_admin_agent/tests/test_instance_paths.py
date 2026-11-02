"""Isolated unit tests for the per-instance on-disk layout (pure, offline).

``instance_paths`` builds ``Path`` objects and creates nothing, so these
tests are deterministic and touch no filesystem.
"""

from __future__ import annotations

from pathlib import Path

from uwazi_admin_agent.domain.instance_paths import instance_paths


def _root(tmp_path: Path) -> Path:
    return tmp_path / "instances"


def test_two_instances_get_different_roots(tmp_path: Path) -> None:
    a = instance_paths("http://a", _root(tmp_path))
    b = instance_paths("http://b", _root(tmp_path))

    assert a.root != b.root


def test_two_instances_get_different_run_task_and_suggestion_paths(tmp_path: Path) -> None:
    a = instance_paths("http://a", _root(tmp_path))
    b = instance_paths("http://b", _root(tmp_path))

    assert a.runs_path != b.runs_path
    assert a.tasks_path != b.tasks_path
    assert a.suggestions_db != b.suggestions_db


def test_the_same_url_gets_the_same_paths(tmp_path: Path) -> None:
    a = instance_paths("http://a", _root(tmp_path))
    b = instance_paths("http://a", _root(tmp_path))

    assert a == b


def test_every_path_lives_under_the_instances_root(tmp_path: Path) -> None:
    root = _root(tmp_path)
    paths = instance_paths("http://a", root)

    for path in (
        paths.root,
        paths.runs_file,
        paths.prompts_path,
        paths.runs_path,
        paths.tasks_path,
        paths.suggestions_db,
    ):
        assert root in path.parents


def test_the_layout_matches_the_documented_tree(tmp_path: Path) -> None:
    root = _root(tmp_path)
    paths = instance_paths("http://a", root)

    assert paths.root == root / paths.instance_id
    assert paths.runs_file == paths.root / "active_run.yaml"
    assert paths.prompts_path == paths.root / "prompts"
    assert paths.runs_path == paths.root / "runs"
    assert paths.tasks_path == paths.root / "tasks"
    assert paths.suggestions_db == paths.root / "suggestions.db"


def test_the_instance_id_is_the_directory_name(tmp_path: Path) -> None:
    paths = instance_paths("http://a", _root(tmp_path))

    assert paths.root.name == paths.instance_id


def test_building_the_paths_creates_nothing(tmp_path: Path) -> None:
    root = _root(tmp_path)

    instance_paths("http://a", root)

    assert not root.exists()
