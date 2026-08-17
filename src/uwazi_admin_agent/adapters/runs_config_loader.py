"""Parse ``active_run.yaml`` for the active run name, then load the per-run
:class:`RunConfig` from ``data/prompts/<active_run>.yaml`` and resolve the
on-disk run folder under ``data/runs/<active_run>/``.

The ``data/`` tree is **package-scoped** (``src/uwazi_admin_agent/data/``),
configured in ``configuration.py`` — it does not collide with the other
packages in the repo.

Mirrors ``browser_agent``'s ``RunsConfigLoader`` (same YAML shapes and the
prompt-snapshot copy) but is **instance-based and path-injected** so tests can
point it at a tmp tree without monkeypatching module constants (the admin
agent's testing policy forbids ``monkeypatch``/mocks). Use
:meth:`RunsConfigLoader.default` for production wiring against
``configuration.py``.

Layout::

    <runs_file>                       # {active_run: <name>}  (`.yaml` suffix optional)
    <prompts_path>/<name>.yaml        # {prompt: ...}  (source of truth)
    <runs_path>/<name>/<name>.yaml    # snapshot copy made when the run folder is resolved
"""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from loguru import logger

from uwazi_admin_agent.configuration import PROMPTS_PATH, RUNS_FILE, RUNS_PATH
from uwazi_admin_agent.domain.run_config import RunConfig


class RunsConfigLoader:
    """Resolve the active run's :class:`RunConfig` and on-disk folder.

    Construct with explicit paths (for tests) or :meth:`default` (for
    production, wired to ``configuration.py``).
    """

    def __init__(self, runs_file: Path, prompts_path: Path, runs_path: Path) -> None:
        self._runs_file: Path = Path(runs_file)
        self._prompts_path: Path = Path(prompts_path)
        self._runs_path: Path = Path(runs_path)

    @classmethod
    def default(cls) -> RunsConfigLoader:
        """Return a loader wired to the ``configuration.py`` path constants."""
        return cls(RUNS_FILE, PROMPTS_PATH, RUNS_PATH)

    def load_active(self) -> RunConfig:
        """Return the active :class:`RunConfig` from ``prompts/<active_run>.yaml``."""
        name = self._load_active_name()
        return self._load_run_config(name)

    def load_active_name(self) -> str:
        """Return the active run name only (does not require the prompt YAML to exist)."""
        return self._load_active_name()

    def load_active_path(self) -> Path:
        """Create the active run's folder and copy the prompt snapshot into it.

        The prompt YAML from ``prompts/<active_run>.yaml`` is copied verbatim
        into ``runs/<active_run>/<active_run>.yaml`` (overwriting any previous
        snapshot) so the run folder records the exact prompt state at execution
        time. The run folder is created if missing.
        """
        name = self._load_active_name()
        return self._run_path(name)

    def _load_active_name(self) -> str:
        """Return the ``active_run`` name, stripping a trailing ``.yaml``."""
        if not self._runs_file.is_file():
            raise FileNotFoundError(f"runs config not found at {self._runs_file}")
        data = yaml.safe_load(self._runs_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "active_run" not in data:
            raise ValueError(f"active_run.yaml must contain an 'active_run' key (got {data!r})")
        return str(data["active_run"]).removesuffix(".yaml")

    def _load_run_config(self, name: str) -> RunConfig:
        """Load a :class:`RunConfig` from ``prompts/<name>.yaml``."""
        config_path = self._prompts_path / f"{name}.yaml"
        if not config_path.is_file():
            raise FileNotFoundError(f"prompt config not found at {config_path} (run {name!r} has no prompt YAML)")
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        return RunConfig.model_validate({"name": name, **data})

    def _run_path(self, name: str) -> Path:
        """Create the run directory and copy the prompt YAML snapshot into it."""
        path = self._runs_path / name
        path.mkdir(parents=True, exist_ok=True)
        self._copy_prompt_snapshot(name, path)
        return path

    def _copy_prompt_snapshot(self, name: str, run_path: Path) -> None:
        """Copy ``prompts/<name>.yaml`` -> ``run_path/<name>.yaml`` if it exists."""
        prompt_yaml = self._prompts_path / f"{name}.yaml"
        if prompt_yaml.is_file():
            _ = shutil.copy2(prompt_yaml, run_path / prompt_yaml.name)
            logger.debug("prompt snapshot copied for run={}", name)
