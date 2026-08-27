from pathlib import Path

# The package directory (``src/uwazi_admin_agent/``). The admin agent's runtime
# data lives in a self-contained ``data/`` subdir here so it is clearly scoped to
# this package and does not collide with the other packages in the repo.
ROOT_PATH: Path = Path(__file__).parent.parent.parent.resolve()
DATA_DIR: Path = ROOT_PATH / "data"

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

# Output token budget per LLM call (pydantic-ai ``ModelSettings["max_tokens"]``).
# The shared ``OllamaAdapter.get_model()`` builds the model with NO ``settings``, so
# pydantic-ai falls back to the provider's default output limit. A long script (a
# multi-group merge can be 100+ lines) plus the ``GeneratedScript`` JSON wrapping +
# reasoning exceeds that default, and pydantic-ai aborts the whole ``generate`` with
# ``UnexpectedModelBehavior: Model token limit (provider default) exceeded before
# any response was generated`` on the final emit. Short scripts fit the default -> the
# failure is intermittent. We set ``max_tokens`` on the admin agent's generation
# ``Agent`` (not the shared adapter) so the fix is scoped to this package. This is a
# per-call OUTPUT budget; it is independent of ``MAX_LLM_CALLS`` (a per-run REQUEST
# count), so raising it does not lower the request budget. 32768 comfortably fits a
# long merge script; it is a ceiling, not a target (shorter outputs cost nothing more).
LLM_MAX_OUTPUT_TOKENS: int = 32768

# Hard cap on ``run_validation_script`` calls per generation turn. The system
# prompt asks the LLM to validate sparingly, but LLMs ignore prose limits and
# loop; this counter (mirrors ``browser_agent``'s ``MAX_VALIDATION_ATTEMPTS``)
# is the backstop that forces the agent to emit a final script instead of
# retrying validation until the ``MAX_LLM_CALLS`` budget is exhausted.
MAX_VALIDATION_ATTEMPTS: int = 5

# Hard cap on ``run_dry_run_script`` calls per generation turn. The dry run is
# the REAL-data rehearsal (real reads, recorded no-op writes) the agent uses to
# prove extraction logic end-to-end before emitting; the counter backstops a
# validation loop the same way MAX_VALIDATION_ATTEMPTS does for the dummy gate.
MAX_DRY_RUN_ATTEMPTS: int = 3

# UsageLimits request cap for the extractor subagent (author_html_extractor).
# The extractor samples files, authors `def extract`, and self-proves it via
# run_validation_script — fewer requests than a full generation run.
MAX_EXTRACTOR_LLM_CALLS: int = 40

# Per-file character truncation for peek_file_text (generation-time HTML
# sampling). HTML supporting files can be huge; the LLM context is not.
MAX_PEEK_CHARS: int = 200_000

# Row locale used when the dummy harness creates/snapshots/reverts dummies. The
# generated script is target-agnostic; the harness fixes the locale for its own
# create/read/revert so the before/after/post-revert raws are comparable.
DUMMY_LANGUAGE: str = "en"

# ES-freshness settle before dummy cleanup (Option A). Uwazi indexes creates/updates
# via ES ``bulk`` (awaited but NOT refreshed) and deletes via ``deleteByQuery``
# (``conflicts: 'proceed'``, ``refresh: true``). A deleteByQuery snapshots the
# refreshed index and skips docs whose version advanced since the snapshot (a newer
# unrefreshed re-index from the dummy gate's revert); the skipped doc reappears
# after the trailing refresh as an orphan (Mongo gone, ES still has it) -> the
# shared index needs ``yarn reindex``. The settle polls ``/api/v2/search`` for each
# dummy's ``editDate`` (bumped on every save - a monotonic "which version is
# refreshed" signal) until it reaches the latest Mongo ``editDate`` seen, so the
# deleteByQuery snapshots the latest version and removes it cleanly (no conflict).
# The dummy gate also skips the no-op revert for unchanged dummies, avoiding the
# re-index entirely. These bound the wait; freshness is adaptive (zero wait once
# the latest version is refreshed), so the timeout only bites on an unresponsive/
# slow ES, in which case the harness proceeds best-effort and records an
# ``es_settle_warning`` (gate correctness is Mongo-based, not ES-based).
ES_SETTLE_TIMEOUT_MS: int = 10_000
ES_SETTLE_POLL_INTERVAL_MS: int = 250

# Safety cap: refuse to execute a run whose touch set exceeds this many entities.
MAX_ENTITIES_PER_RUN: int = 10000

# Number of entities to apply per batch during execute. Consumed in Phase 5.
EXECUTE_BATCH_SIZE: int = 50

# Default on-error policy for the execute step (Phase 6). Parsed into
# ``OnErrorPolicy`` by the CLI. "stop" leaves the partial run for the operator
# to revert explicitly; "stop-and-revert" auto-reverts whatever was backed up
# before the error. Kept as a string so ``configuration.py`` stays stdlib-only.
DEFAULT_ON_ERROR_POLICY: str = "stop"
