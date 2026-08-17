from pathlib import Path

# The package directory (``src/uwazi_admin_agent/``). The admin agent's runtime
# data lives in a self-contained ``data/`` subdir here so it is clearly scoped to
# this package and does not collide with the other packages in the repo.
PACKAGE_DIR: Path = Path(__file__).parent.resolve()
DATA_DIR: Path = PACKAGE_DIR / "data"

# Run mechanism (mirrors ``browser_agent``): ``active_run.yaml`` names the
# active run; its prompt YAML lives in ``data/prompts/<name>.yaml``; per-run
# artifacts (prompt snapshot, generated script, manifest, snapshots) live under
# ``data/runs/<name>/``.
RUNS_FILE: Path = DATA_DIR / "active_run.yaml"
PROMPTS_PATH: Path = DATA_DIR / "prompts"
RUNS_PATH: Path = DATA_DIR / "runs"

# Default on-disk backup-store root, under the operator's working directory.
# (Phase 4 will decide whether to align this onto ``RUNS_PATH`` so one run = one
# folder holding prompt snapshot + script + manifest + snapshots.)
DEFAULT_STORE_DIR: Path = (Path(".uwazi_admin_agent") / "runs").resolve()

# Safety cap: refuse to execute a run whose touch set exceeds this many entities.
MAX_ENTITIES_PER_RUN: int = 1000

# Number of entities to apply per batch during execute. Consumed in Phase 5.
EXECUTE_BATCH_SIZE: int = 50
