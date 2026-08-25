from pathlib import Path

import pytest

from uwazi_admin_agent.adapters.runs_config_loader import RunsConfigLoader
from uwazi_admin_agent.domain.run_config import RunConfig


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_loader(root: Path) -> RunsConfigLoader:
    return RunsConfigLoader(
        runs_file=root / "active_run.yaml",
        prompts_path=root / "prompts",
        runs_path=root / "runs",
    )


def test_load_active_returns_run_config_from_prompt_yaml(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "active_run: repair-titles\n")
    _write(tmp_path / "prompts" / "repair-titles.yaml", "prompt: Set every Court title to uppercase\n")

    cfg = _make_loader(tmp_path).load_active()

    assert cfg == RunConfig(name="repair-titles", prompt="Set every Court title to uppercase")


def test_load_active_strips_trailing_yaml_suffix(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "active_run: repair-titles.yaml\n")
    _write(tmp_path / "prompts" / "repair-titles.yaml", "prompt: p\n")

    cfg = _make_loader(tmp_path).load_active()

    assert cfg.name == "repair-titles"


def test_load_active_path_creates_run_folder_and_copies_prompt_snapshot(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "active_run: repair-titles\n")
    prompt_yaml = tmp_path / "prompts" / "repair-titles.yaml"
    _write(prompt_yaml, "prompt: Set every Court title to uppercase\n")

    run_path = _make_loader(tmp_path).load_active_path()

    assert run_path == tmp_path / "runs" / "repair-titles"
    assert run_path.is_dir()
    snapshot = run_path / "repair-titles.yaml"
    assert snapshot.is_file()
    assert snapshot.read_text(encoding="utf-8") == prompt_yaml.read_text(encoding="utf-8")


def test_load_active_path_is_idempotent_and_overwrites_snapshot(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "active_run: r1\n")
    _write(tmp_path / "prompts" / "r1.yaml", "prompt: first\n")

    loader = _make_loader(tmp_path)
    run_path = loader.load_active_path()

    _write(tmp_path / "prompts" / "r1.yaml", "prompt: second\n")
    loader.load_active_path()

    assert (run_path / "r1.yaml").read_text(encoding="utf-8") == "prompt: second\n"


def test_load_active_raises_when_runs_file_missing(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        _make_loader(tmp_path).load_active()


def test_load_active_raises_when_active_run_key_missing(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "other: x\n")

    with pytest.raises(ValueError):
        _make_loader(tmp_path).load_active()


def test_load_active_raises_when_prompt_yaml_missing(tmp_path) -> None:
    _write(tmp_path / "active_run.yaml", "active_run: no-prompt\n")

    with pytest.raises(FileNotFoundError):
        _make_loader(tmp_path).load_active()


def test_default_loader_is_wired_to_configuration_paths() -> None:
    from uwazi_admin_agent import configuration

    loader = RunsConfigLoader.default()

    assert loader._runs_file == configuration.RUNS_FILE
    assert loader._prompts_path == configuration.PROMPTS_PATH
    assert loader._runs_path == configuration.RUNS_PATH


def test_default_paths_are_package_scoped_under_uwazi_admin_agent_data() -> None:
    from uwazi_admin_agent import configuration

    # The runtime data tree lives inside the package, not at the repo root,
    # so it cannot collide with uwazi_agent / uwazi_api data.
    package_data = configuration.ROOT_PATH / "data"

    assert configuration.DATA_DIR == package_data
    assert configuration.RUNS_FILE == package_data / "active_run.yaml"
    assert configuration.PROMPTS_PATH == package_data / "prompts"
    assert configuration.RUNS_PATH == package_data / "runs"
    assert configuration.ROOT_PATH.name == "python_uwazi_API"
