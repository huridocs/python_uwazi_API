import os
from pathlib import Path

# The package directory (``src/uwazi_admin_agent/``). The admin agent's runtime
# data lives in a self-contained ``data/`` subdir here so it is clearly scoped to
# this package and does not collide with the other packages in the repo.
ROOT_PATH: Path = Path(__file__).parent.parent.parent.resolve()
DATA_DIR: Path = ROOT_PATH / "data"

# Per-instance data root. ``UWAZI_URL`` may name ONE instance or a
# comma-separated LIST of them; every instance gets its own tree under
# ``data/instances/<slug>/`` (see ``domain/instance_paths.py``):
# ``active_run.yaml`` names the active run, its prompt YAML lives in
# ``prompts/<name>.yaml``, per-run artifacts (prompt snapshot, generated
# script, manifest, snapshots) live under ``runs/<name>/``, tasks in
# ``tasks/<task_id>.yaml`` and the suggestions in ``suggestions.db``.
INSTANCE_IDS_DIR: Path = DATA_DIR / "instances"

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
LLM_MAX_OUTPUT_TOKENS: int = 262144

# UsageLimits request cap for the extractor subagent (author_html_extractor).
# The extractor samples files, authors `def extract`, and self-proves it in
# process — fewer requests than a full generation run.
MAX_EXTRACTOR_LLM_CALLS: int = 40

# Per-file character truncation for peek_file_text (generation-time HTML
# sampling). HTML supporting files can be huge; the LLM context is not.
MAX_PEEK_CHARS: int = 200_000

# --- extraction strategy benchmark (benchmark_extractor) ----------------------
#
# Scoring a candidate extractor needs entities whose target properties ALREADY
# hold a known value (that value is the ground truth). Sampling is bounded like
# every other generation-time read: a handful of entities answers "does this
# strategy work?" — a full scan is the script's job, not the benchmark's.
MAX_BENCHMARK_SAMPLES: int = 30
# Per-sample text fed to the candidate extractor: the same head-and-tail window
# the peek tools use, so the benchmark exercises the same inputs a script sees.
MAX_BENCHMARK_CHARS: int = 200_000

# --- mismatch-driven extractor refinement (benchmark_extractor) -----------------
#
# When a benchmarked strategy still mismatches, the generation agent may go back
# and re-author the extractor. The refinement loop is BOUNDED so a turn lands in
# a reasonable time: a refinement step consumes real sample reads (each one a
# live raw-entity + file fetch) plus authoring LLM calls, so unbounded "improve
# until perfect" is both slow and overfitting-prone. Hard cap per turn:
MAX_REFINEMENTS_PER_TURN: int = 3
# Mismatch examples fed back to the extractor author, per refinement step. The
# evidence must stay a manageable SUBSET, not the full list: a handful of
# varied failures is enough to diagnose a strategy, and pasting every mismatch
# invites the author to special-case each one instead of fixing the rule.
MAX_REFINEMENT_MISMATCHES: int = 5
# When the samples are too few to judge a refinement at all, refinement is
# withheld rather than offered: an "improved" extractor validated against one
# or two entities is noise, not evidence. The full benchmark keeps running
# (its sample size is the operator's chosen floor); this only gates the
# refinement loop.
MIN_SAMPLES_FOR_REFINEMENT: int = 5

# --- external URL fetching (source-page HTML extraction) ---------------------
#
# The agent's read boundary is extended to operator-supplied ``http(s)`` URLs (an
# entity's Source Page URL — a ``link`` metadata property — or a URL attachment),
# so extraction runs even when an entity has no uploaded supporting file. Because
# these URLs are external and unbounded, every fetch is fenced by a scheme
# allow-list (http/https only), a timeout, and a byte cap. Truncation is a
# separate, authoring-time concern (``URL_FETCH_MAX_CHARS``) — the exec helper
# returns the full (byte-capped) text to the pure ``extract`` function.
URL_FETCH_TIMEOUT_SECONDS: float = 15.0
URL_FETCH_MAX_BYTES: int = 5 * 1024 * 1024
# Authoring-time character truncation for peek_url_text (LLM-context bound).
URL_FETCH_MAX_CHARS: int = 200_000

# Row locale used where no row language is known (a language-independent row's
# link, a default read). The generated script is target-agnostic.
DUMMY_LANGUAGE: str = "en"

# --- controlled LLM usage (prediction passes + script helpers) -----------------
#
# Generated scripts may call the LLM per entity (extraction/enrichment) through
# the bound ``llm_complete`` / ``llm_map`` helpers. Every such call is charged
# against a user-approved :class:`LlmBudget`: the DOLLAR cap is the
# AUTHORITATIVE limit (enforced before each call against the price-derived
# cost, aborting the pass cleanly); the request count is a backstop against
# cheap-call runaway loops. The prices below convert usage into that money —
# placeholders pending real ollama.com pricing, overridable via the environment.
LLM_PRICE_INPUT_PER_MTOK: float = float(os.environ.get("LLM_PRICE_INPUT_PER_MTOK", "0.14"))
LLM_PRICE_OUTPUT_PER_MTOK: float = float(os.environ.get("LLM_PRICE_OUTPUT_PER_MTOK", "0.55"))

# Entities per ``llm_map`` call: the batch size the helper splits its input list
# into, so one LLM request covers many entities (and the request cap is spent
# per batch, not per entity).
LLM_MAP_BATCH_SIZE: int = 10

# Default ceilings for a prediction pass, shown in the cost dialog and used as
# the enforced cap unless the operator tightens them before approving. The
# dollar cap is the one the operator edits; the request backstop bounds a
# pathological cheap-call loop.
PREDICTION_DEFAULT_MAX_COST: float = 10.0
PREDICTION_DEFAULT_MAX_REQUESTS: int = 2000

# Default hard caps for ONE prompt-improvement chat turn (the chat's LLM call).
# The turn is a single tool-free request; the caps are the enforced backstop.
IMPROVE_DEFAULT_MAX_COST: float = 0.05
IMPROVE_DEFAULT_MAX_REQUESTS: int = 1

# Hard caps on the GENERATED SCRIPT's own llm_complete/llm_map calls (the
# wizard's caps step; enforced by the planning pass's LlmLedger). They apply
# ONLY to the script's tool calls — never to script generation, which is
# capped by MAX_LLM_CALLS/LLM_MAX_OUTPUT_TOKENS. A script that computes in
# pure Python spends nothing under them.
#
# The defaults are ZERO on both dimensions (deny-by-default): a wizard run
# leaves them untouched, so the script's LLM tools abort cleanly on their
# first call. The operator opts in by raising either cap in the wizard.
SCRIPT_DEFAULT_MAX_COST: float = 0.0
SCRIPT_DEFAULT_MAX_REQUESTS: int = 0

# The wizard's AI-allowance prefill when the operator LETS the task use the AI.
# The checkbox is the opt-in, so a checked box must not prefill the
# deny-by-default zero: this small cap covers a typical summary-style run.
WIZARD_DEFAULT_AI_ALLOWANCE: float = 1.0

# Rows per page in the review table (server-side pagination over 10k+ rows).
REVIEW_PAGE_SIZE: int = 25

# The per-instance SQLite file holding every run's predictions (suggestions)
# and prediction-attempt rows lives under the instance's own tree
# (``data/instances/<slug>/suggestions.db``) — one DB (not per-run files)
# because the review grid needs SQL-side filtering/pagination/aggregation over
# ~10k rows. Multistep tasks: one YAML per task under the instance's
# ``tasks/`` directory.

# Safety cap: refuse to execute a run whose touch set exceeds this many entities.
MAX_ENTITIES_PER_RUN: int = 10000

# Safety cap on a generated script's READ volume for one pass. A script can call
# query_entities repeatedly; without a bound one pass can page the whole instance
# into memory (and into the LLM's context). 20k covers a 10k-entity run with the
# discovery reads around it.
MAX_ENTITIES_READ_PER_PASS: int = 20_000

# Safety cap on dry-run recorder rows for one pass. A runaway loop that keeps
# writing is stopped by the wall-clock deadline too, but this bounds the memory
# a recorded pass can accumulate before that fires.
MAX_DRY_RUN_RECORDS: int = 200_000

# Number of entities to apply per batch during execute. Consumed in Phase 5.
EXECUTE_BATCH_SIZE: int = 50

# Ids per ``POST /api/entities/multipleupdate`` call. Uwazi rejects more than
# 1000 ("You must provide at most 1000 entity ids for multiple update"), and the
# endpoint is all-or-nothing per call, so the review table's homogeneous
# property writes are chunked at this size and fall back per entity on refusal.
BULK_UPDATE_MAX_IDS: int = 1000

# Default on-error policy for the execute step (Phase 6). Parsed into
# ``OnErrorPolicy`` by the CLI. "stop" leaves the partial run for the operator
# to revert explicitly; "stop-and-revert" auto-reverts whatever was backed up
# before the error. Kept as a string so ``configuration.py`` stays stdlib-only.
DEFAULT_ON_ERROR_POLICY: str = "stop"

# --- persistent cross-task file/entity cache ----------------------------------
#
# A generated extraction script does an N+1 read per entity (one raw-entity GET
# + one GET per supporting file), repeated across a turn's up-to-4 dry-run
# passes, the execute pass, and every later task (~2h per full pass on the
# production instance). The cache is rooted under DATA_DIR so it survives
# across processes, tasks, and runs, and is namespaced per Uwazi instance URL
# (the same sharedId on two instances means different data).
#
# File bytes are immutable per Uwazi storage filename (minted fresh on every
# upload — app/api/files/filesystem.ts — and never rewritten), so they cache
# forever EXCEPT that our own file deletes evict the deleted files' entries
# (the delete helpers own the eviction; it is lossless — the backup store
# captured the bytes before the delete). Entity raws are mutable (agent
# writes + direct human edits), so they carry a TTL plus write-path
# invalidation (CachedEntityRepository on save/delete; BackupIntercept for
# sandbox CRUD writes AND files-collection mutations — deletes + revert
# re-uploads; VerifyRevertUseCase refetches fresh). Set
# UWAZI_ADMIN_FILE_CACHE=0 to bypass the cache entirely for debugging.
FILE_CACHE_ENABLED: bool = True
FILE_CACHE_DIR: Path = DATA_DIR / "file_cache"
# Hard cap on total cached bytes (files + raws) per instance. The 15k x 2 x
# 60KB supporting-file working set is ~1.8 GB plus ~0.5 GB of raw JSONs, so
# 4 GiB keeps a full pass resident; the amortized LRU eviction trims older
# entries whenever the cap is exceeded.
FILE_CACHE_MAX_BYTES: int = 4 * 1024 * 1024 * 1024
# Puts between amortized eviction scans (one scan drops expired raws, then
# oldest-mtime entries until back under 90% of FILE_CACHE_MAX_BYTES).
FILE_CACHE_EVICT_SCAN_INTERVAL: int = 256
# Freshness window for cached entity raws, in seconds. Human edits made
# directly in Uwazi are invisible to write-path invalidation, so this bounds
# how stale a cached raw can be against a direct human edit.
ENTITY_CACHE_TTL_SECONDS: float = 600.0
# When True (default), BackupIntercept snapshot fetches bypass the raw cache
# (invalidate-then-refetch) so backups always capture live server truth — the
# revert path's correctness outweighs execute speed. The script's own reads
# still hit the cache. Flip to False to also serve snapshots from cache.
ENTITY_CACHE_FRESH_SNAPSHOTS: bool = True

# --- auto-throttled parallel bulk helpers --------------------------------------
#
# The generated scripts' bulk cost is serial HTTP: the update/create ports loop
# per entity inside ONE port call (one request per entity, minutes per 10 000 on
# a remote production instance), and the raw repositories block per fetch. The
# ``*_parallel`` script helpers run that work on up to THROTTLE_MAX_WORKERS
# worker threads and adapt the worker allowance after EVERY batch: a load
# complaint (429 / rate-limit text) drops it by one toward THROTTLE_MIN_WORKERS,
# and THROTTLE_PROMOTION_STREAK consecutive clean batches raise it by one back
# toward the cap. "4 processes" = these 4 concurrent workers: the sandbox
# helpers are closures (unpickleable) and share one logged-in session, so
# threads — not OS processes — are the parallel unit.
THROTTLE_MAX_WORKERS: int = 4
THROTTLE_MIN_WORKERS: int = 1
# Consecutive clean batches required before the allowance grows by one.
THROTTLE_PROMOTION_STREAK: int = 3
# Breather after a load complaint (the per-request retry layer already honors
# Retry-After; this lets the instance drain before the NEXT batch arrives).
THROTTLE_PENALTY_PAUSE_SECONDS: float = 1.0
# Entities per port call inside the parallel write helpers — the same "~50"
# granularity the sequential contract teaches, now one task per chunk.
PARALLEL_WRITE_BATCH_SIZE: int = EXECUTE_BATCH_SIZE

AI_TRANSLATED_MARKER = "(AI translated) "
