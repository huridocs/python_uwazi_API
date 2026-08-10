"""``uwazi_admin_agent`` — a batch, operator-driven admin agent for Uwazi.

Operating model:

- The LLM **plans**, deterministic code **executes**. The LLM's only job is to turn
  a natural-language prompt into a declarative ``MigrationPlan`` (structured ops
  from a fixed schema). It returns data, never code, and cannot run arbitrary
  code or call Uwazi directly.
- A migration run proceeds: plan -> touch-set computation -> dry-run ->
  snapshot -> execute -> verify -> (revert on demand).
- Backup & revert are **data**, not generated code: a ``Snapshot`` is the exact
  raw entity JSON Uwazi returned; a ``Manifest`` records per run what was
  modified, created, and rewired. Revert restores snapshots, deletes created
  entities, and restores rewired relationships to their before-state.
- **Raw fidelity**: backup/restore use raw entity dicts, not validated models,
  so round-tripping drops no fields.
- The executor backs up the **complete touch set** (direct targets plus every
  entity whose relationships get rewired) and refuses to execute if the
  snapshot set does not cover it.

This package depends on ``uwazi_api`` for raw access and does **not** depend on
``uwazi_agent`` (different concern, lifecycle, and safety contract).
"""
