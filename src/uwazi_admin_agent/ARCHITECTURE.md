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

## Validation set (holdout performance score)

A run may carry a **validation set**: a frozen sample of entities the authoring
side must never read, used to score the script honestly.

- `domain/validation.py` — the pure model: `ValidationSet` (ids + labels frozen
  at creation), `ValidationScore`, the metric rule (micro-F1 for
  `multiselect`/`relationship`, normalized accuracy otherwise), and
  `score_validation`.
- `adapters/validation_store.py` — `validation.yaml` + `validation_score.json`
  beside the run's manifest.
- `adapters/validation_holdout.py` — the deny-list wrappers. `build_runtime`
  mounts them on the AUTHORING ports (`deps.entity_api`,
  `deps.entity_repository`, `deps.segmentation_repository`) when the run has a
  holdout; the runtime's own ports stay raw for the evaluation and apply paths.
- `use_cases/validation_sampling.py` — the one read that DEFINES the set
  (automatic sample, or the LLM-authored custom selector), run before
  generation.
- `domain/validation_selector.py` + `use_cases/generate_validation_selector_use_case.py`
  — the custom picker. The operator DESCRIBES the set (property filter, date
  window, explicit shared ids) and the LLM writes a small read-only snippet over
  `query_entities`/`query_entities_full`; `script_exec_namespace/selector.py`
  is the namespace it runs in (reads only, no writers, no LLM helpers, no
  `random`).
- `use_cases/evaluate_validation_use_case.py` — scores the committed script by
  running it with the PRODUCTION read surface (no entity allow-list) and keeping
  only the rows that address a held-out entity; writes are recorded, never
  applied. It must see the same instance the planning pass sees: a
  classify-by-relationship script resolves its names against a catalog template,
  and scoping reads to the holdout starved that catalog — every held-out entity
  then recorded `skipped` and a working script scored 0. The script's own
  `llm_complete`/`llm_map` calls are bound exactly as the planning pass binds
  them: under the run's approved budget and a fresh ledger (a caller that wires
  none gets a deny-all ceiling). Evaluating with them unbound made every
  LLM-driven row `skipped` and scored a working script at 0. A row the script
  produced no measured value for still appears, carrying the frozen label and
  the script's own skip reason.
- `use_cases/script_exec_namespace/scope.py` — `_scoped_entity_reads(...,
  exclude=...)` and `_excluded_entity_reads`: the PRODUCTION planning pass also
  drops the held-out ids, so the review table never proposes writes for them.
  `strip_measured_property` / `_measured_property_guarded_reads` are the second
  deny-list: the MEASURED property's stored value is removed from every
  discovery read (`query_entities`, `query_entities_full`) unless the operator
  declared that property as an input (`ValidationSet.input_properties`). A
  proposal computed from the value it replaces scores perfectly while proving
  nothing — that is how a validation sample ended up carrying a value the
  document never mentioned.

Rows of the validation pass carry `Suggestion.is_validation`. They are evidence,
never work: `payloads.pushable_rows`, the store's selection projection, the
checkboxes and the table's Run all refuse them. The ONE way out is an explicit
promotion (`promote_validation_row`), which shrinks the set (bumping its
version, so the stored score reads `stale`) and relabels the entity's rows as
regular PENDING work. A production reconcile never deletes a validation row.

The measured property is ONE per run: the always-visible Performance line at the
top of the review page states which, as a percentage with the metric named in its
caption, and the review table's property filter carries no score at all (two
numbers would compete).

The wizard's custom test set is a PROMPT, not a button: the operator describes
the filter and the maximum entity count, and `build_validation_set` asks the LLM
for the read-only selector at task-creation time (so filling the wizard never
waits on a model turn). Validation rows are shown as the `validate` operation
with Uwazi's frozen label as their current value, so the operator can judge each
suggestion against the value the score measures.

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
  - The wizard opens with the task type: **Fill a property**, **Create
    entities**, or **Custom task**. Guided tasks use the pure conditional
    navigation in `domain/guided_flow.py`: one question per screen, automatic
    advancement for radio choices, and only relevant follow-up questions.
  - Fill a property covers classification: the target property's Uwazi type
    (`select`/`multiselect`/`relationship`) switches the flow to the
    classification sub-flow (existing thesaurus terms or linked entities, plus
    the no-match fallback); any other type gets the extraction sub-flow
    (formatting rules only — there is NO existing-value question: the proposal
    is computed from the task's input, and keeping or replacing a stored value
    is the operator's per-row decision in the review table).
  - Create entities branches on what the new entities are based on: existing
    entities' properties (one new entity per source entity), existing entities'
    documents, or instructions unrelated to the instance. The last one asks for
    a count + instructions and no source scope.
- `drivers/web/review_page/` — the review page:
  - `core.py` — table, filters, actions, selection, drift
  - `entity_window.py` — entity detail window, files, labels/types
  - `value_pickers.py` — select/relationship value pickers (titles + search)
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
