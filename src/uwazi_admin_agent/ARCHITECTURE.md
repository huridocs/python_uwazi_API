# `uwazi_admin_agent` architecture

A batch, operator-driven admin agent for Uwazi. The code follows a hexagonal
(Clean Architecture) layout; the rules below are what keep changes local and
testable.

## Layers

```
domain/      value objects, pure rules, no I/O, no framework imports
ports/       abstract seams (ABC / Protocol), no implementation
adapters/    Uwazi HTTP, SQLite, filesystem, LLM concrete implementations
use_cases/   orchestration over ports; no NiceGUI, no web imports
drivers/     composition roots + CLI + NiceGUI pages
```

Dependency direction is one-way: `domain ← ports ← adapters/use_cases ← drivers`.
Do not import upward. `drivers/web` may import use cases and adapters; use cases
must not import drivers or adapters.

## End-to-end flow

```
task / CLI / web
  -> clarify prompt            use_cases/clarify_*_use_case.py
  -> generate script           use_cases/generate_script_use_case.py
  -> automatic selection scan  use_cases/generate_predictions_use_case.py
  -> review table              adapters/sqlite_suggestion_store/
  -> apply selected rows       use_cases/apply_suggestions_use_case/
  -> revert                    use_cases/revert_run_use_case/
  -> verify                    use_cases/verify_revert_use_case.py
```

The generated script runs inside `use_cases/script_exec_namespace/`; it only
sees the bound helpers, never free-form network or DB access.

## Web driver packages

The old three god files are now packages. Public imports are unchanged because
each `__init__.py` re-exports the original names.

- `drivers/web/run_service/` — the web service layer:
  - `models.py` — result/exception types
  - `wiring.py` — instance paths + suggestion-store construction
  - `runs.py` — list/get/delete/rename runs, audit history, cache
  - `documents.py` — segmentation + document bytes
  - `chat.py` — clarify/compact prompt
  - `generation.py` — create/generate/regenerate, prediction passes
  - `table.py` — refresh from Uwazi + preflight
  - `review.py` — review-table reads, filters, discard/restore
  - `apply.py` — apply the reviewed rows
  - `tasks.py` — multistep tasks
  - `revert.py` — per-entity/per-property/revert selection
  - `drift.py` — thin wrappers over `use_cases/drift_service.py`
- `drivers/web/app/` — the NiceGUI app:
  - `state.py`, `broadcast.py`, `login.py`, `runs_table.py`,
    `dialogs.py`, `wizard.py`, `guided_form.py`, `page.py`
  - The wizard opens with the task type. Guided tasks use the pure conditional
    navigation in `domain/guided_flow.py`: one question per screen, automatic
    advancement for radio choices, and only relevant follow-up questions.
- `drivers/web/review_page/` — the review page:
  - `core.py` — table, filters, actions, selection, drift
  - `entity_window.py` — entity detail window, files, labels/types
  - `page.py` — route + page layout
  - `dialogs.py` — confirm/reason dialogs

## Engine packages split by concern

- `use_cases/script_exec_namespace/` — `constants`, `scope`, `reads`, `files`,
  `dry_run`, `namespace`
- `adapters/sqlite_suggestion_store/` — `sql`, `base`, `passes`, `writes`,
  `reads`, `drift`; `store.py` composes the mixins
- `use_cases/apply_suggestions_use_case/` — `errors`, `payloads`, `use_case`
- `use_cases/revert_run_use_case/` — `base`, `merge`, `entity`, `files`,
  `audit`; `use_case.py` composes the mixins

## Adding a new operation (checklist)

Operations are a deliberate fan-out. Touch every place that must agree:

1. `domain/suggestion.py` — extend `SuggestionOp`, the op sets, display labels.
2. `domain/suggestion_scoring.py` — how the planning pass records the op.
3. `use_cases/script_exec_namespace/` — the helper the script calls (and its
   dry-run/no-op twin).
4. `use_cases/dry_run_script_use_case.py` — if the op needs a new record shape.
5. `use_cases/apply_suggestions_use_case/payloads.py` — apply payload/grouping.
6. `use_cases/apply_suggestions_use_case/use_case.py` — write dispatch.
7. `use_cases/revert_run_use_case/` — undo ordering and restore path.
8. `use_cases/system_prompt.py` — teach the generation LLM the contract.
9. `drivers/web/review_view.py` — label, color, action, editability.
10. `drivers/web/review_page/` — row/suboperation UI if it needs special rendering.
11. `adapters/sqlite_suggestion_store/` — filters/grouping only if the op needs it.
12. Tests: pure payload/recording tests, plus an `_e2e.py` guarded scenario.

## Tests

```bash
# fast, offline (the default)
uv run python -m pytest src/uwazi_admin_agent/tests -m "not e2e" -q

# one file while iterating
uv run python -m pytest src/uwazi_admin_agent/tests/test_<module>.py -v

# live, localhost-only, opt-in
just test_e2e
```

The unit suite is intentionally fast and mostly pure. Keep new tests in that
tier unless they genuinely need a live Uwazi instance; the localhost guard in
`AGENTS.md` applies to every E2E file.
