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

# The backup store (``FilesystemBackupStore``) is rooted at ``RUNS_PATH`` so one
# run = one folder holding prompt snapshot + script + manifest + snapshots
# (resolved in Phase 5; the Phase-1 ``DEFAULT_STORE_DIR`` placeholder is removed).

# Hard cap on LLM requests per script-generation run (passed to pydantic-ai's
# ``UsageLimits``). Mirrors ``browser_agent``'s ``MAX_LLM_CALLS``; generous because
# discovery (``query_entities``) + script writing both consume requests.
MAX_LLM_CALLS: int = 70

# Hard cap on ``run_validation_script`` calls per generation turn. The system
# prompt asks the LLM to validate sparingly, but LLMs ignore prose limits and
# loop; this counter (mirrors ``browser_agent``'s ``MAX_VALIDATION_ATTEMPTS``)
# is the backstop that forces the agent to emit a final script instead of
# retrying validation until the ``MAX_LLM_CALLS`` budget is exhausted.
MAX_VALIDATION_ATTEMPTS: int = 5

# Row locale used when the dummy harness creates/snapshots/reverts dummies. The
# generated script is target-agnostic; the harness fixes the locale for its own
# create/read/revert so the before/after/post-revert raws are comparable.
DUMMY_LANGUAGE: str = "en"

# Safety cap: refuse to execute a run whose touch set exceeds this many entities.
MAX_ENTITIES_PER_RUN: int = 1000

# Number of entities to apply per batch during execute. Consumed in Phase 5.
EXECUTE_BATCH_SIZE: int = 50

# Default on-error policy for the execute step (Phase 6). Parsed into
# ``OnErrorPolicy`` by the CLI. "stop" leaves the partial run for the operator
# to revert explicitly; "stop-and-revert" auto-reverts whatever was backed up
# before the error. Kept as a string so ``configuration.py`` stays stdlib-only.
DEFAULT_ON_ERROR_POLICY: str = "stop"
