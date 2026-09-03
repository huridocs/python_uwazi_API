"""``uwazi_admin_agent`` — a batch, operator-driven admin agent for Uwazi.

Operating model:

- The LLM **generates a Python script** that performs the bulk change, using the
  CRUD helpers reused from ``uwazi_agent`` (create/update/delete/publish
  entities, create_relationships, query_entities, query_entities_full). It
  does not run free-form network or DB code — only the bound helpers.
- Bulk work runs **auto-throttled in parallel**: the bound ``*_parallel``
  variants (update/create/create_relationships writes, entity-file/file-byte
  reads) run on up to ``THROTTLE_MAX_WORKERS`` (4) concurrent workers, backing
  off toward 1 when Uwazi complains about load (429/rate-limit) and climbing
  back after clean batches — one shared ``ThrottleController`` per execute or
  dry-run pass. The SAME names bind in dummy/dry-run mode with identical
  shapes, so one script validates unchanged.
- A run proceeds: prompt (active_run.yaml) -> generate script -> simulate
  against dummy entities in the real instance -> backup the real touch set ->
  execute against real entities -> (revert on demand).
- **Dummy-entity gate**: a script must pass against dummies (correct outcome
  AND revert restores exact original raw state) before real data is touched.
- **Backup is CRUD-intercepted**: the real-execution CRUD helpers snapshot the
  raw before-state of every entity they modify/delete before applying, so the
  snapshot set covers the touch set by construction.
- Backup & revert are **data**, not generated code: a snapshot is the exact raw
  entity JSON Uwazi returned; a manifest records per run the prompt, the script,
  what was modified, and what was rewired. Revert restores snapshots and rewired
  relationships to their before-state.
- **Raw fidelity**: backup/restore use raw entity dicts, not validated models,
  so round-tripping drops no fields.

This package reuses ``uwazi_agent``'s tools/ports/adapters (imported, not
copied) and depends on ``uwazi_api`` for raw access. It does not modify
``uwazi_api``.
"""
