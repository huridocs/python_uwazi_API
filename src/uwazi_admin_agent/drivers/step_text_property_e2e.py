"""``uwazi-admin-agent text-property-e2e`` — a live end-to-end exercise of the
PER-LANGUAGE TEXT-property update path, driven exactly the way the UI drives it.

The UI never "runs a script against Uwazi". It walks a multistep TASK, and this
driver walks the SAME task against the SAME live instance, through the SAME
endpoints:

1. **Create the task** — the "New Task" wizard writes ``data/prompts/<run>.yaml``
   + ``data/active_run.yaml`` and generates the script for the prompt
   (:func:`create_and_generate`), which then runs the automatic planning pass
   that fills the review table. The task definition itself is persisted by
   :class:`~uwazi_admin_agent.use_cases.task_use_case.TaskUseCase` (one step =
   one run).
2. **Plan** — the planning pass records the would-be writes (the review table's
   rows). One row per (entity, LANGUAGE).
3. **Run** — the review page's "Run" button pushes the CHECKED
   (sharedId, language) rows through the deterministic apply heuristic
   (:func:`apply_outcomes`), which writes through the backup intercept, so every
   write is revertable.
4. **Revert** — the task page's "Revert task" button walks the revert plan in
   REVERSE order (:meth:`TaskUseCase.revert_task`), undoing every executed
   step of the task.
5. **Verify** — the post-revert verification re-reads the live rows and compares
   them with the pre-run snapshots.

The USE CASE under test: updating a property of type ``text`` on the SAME entity
in TWO languages (``en`` and ``es``) with a DIFFERENT value per language, on
2–4 entities at a time, and undoing it. The interesting part is that Uwazi
stores one entity-language ROW per translation (same ``sharedId``, different
``_id``), a text property holds exactly ONE value per row ("Text Property only
accepts a single value."), and the write's ``locale`` cookie picks the row — so
an ``en`` write must never land on the ``es`` row and vice versa.

THREE tasks are created, each a different angle on that use case:

- **task 1 — two steps, one per language** (the multistep-task shape): step 1
  writes the ``en`` rows, step 2 writes the ``es`` rows of the SAME entities.
  Proves the two language rows are written independently with different values
  and that the whole-task revert undoes both.
- **task 2 — one step, both languages** (one run covering both rows of every
  entity): the review table carries two rows per entity and the Run pushes
  both. Proves a single run can write both rows without merging them.
- **task 3 — one step, a different batch size per language** (2 in one, 4 in the
  other): proves the language groups are independent in COUNT too.

IDEMPOTENT BY CONSTRUCTION: the driver never invents its own data. It picks
LIVE entities from the real instance, captures their current values BEFORE any
write, and — whatever happens — restores exactly those values at the end (the
revert path, or a direct restore for anything the revert did not cover). A
second run therefore starts from the same state as the first, and the report
says so explicitly.

The script NEVER uses mocks or a fake instance: it drives the real instance
configured through ``UWAZI_URL`` with the operator's credentials (``admin``/
``admin`` by default).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from uwazi_admin_agent.configuration import PROMPTS_PATH, RUNS_FILE, TASKS_PATH
from uwazi_admin_agent.domain.manifest import RunStatus
from uwazi_admin_agent.domain.suggestion import SuggestionFilter
from uwazi_admin_agent.drivers.runtime import build_backup_store
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_suggestion_store,
    create_and_generate,
    current_attempt,
    revert_run,
)
from uwazi_admin_agent.use_cases.task_use_case import TaskUseCase

# The languages the use case is about. Uwazi keeps one entity-language ROW per
# translation, and the write's ``locale`` cookie picks the row.
LANGUAGES: tuple[str, str] = ("en", "es")

# The template the target entities live under.
DEFAULT_TEMPLATE: str = "DOCUMENT"

# How many entities one task touches. The use case is "2 to 4 entities at a
# time", so the driver varies it across the three tasks.
DEFAULT_ENTITIES_PER_LANGUAGE: int = 2


# --- the report ---------------------------------------------------------------


@dataclass
class Check:
    """One verified expectation about the live instance."""

    name: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "PASS" if self.ok else "FAIL"
        return f"  [{mark}] {self.name}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class StepReport:
    """What one task STEP (one run) did and what the checks said about it."""

    title: str
    run_id: str
    prompt: str
    pairs: list[tuple[str, str]] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name=name, ok=ok, detail=detail))
        return ok


@dataclass
class TaskReport:
    """What one TASK did: its steps, and the checks over the whole task."""

    task_id: str
    name: str
    steps: list[StepReport] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(step.ok for step in self.steps) and all(check.ok for check in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name=name, ok=ok, detail=detail))
        return ok


# --- live instance access -----------------------------------------------------


def _client(user: str, password: str) -> Any:
    """A logged-in ``UwaziClient`` against the configured instance."""
    from uwazi_api.client import UwaziClient

    url = os.environ.get("UWAZI_URL")
    if not url:
        raise RuntimeError("UWAZI_URL is not set — point it at the instance to exercise")
    return UwaziClient(user=user, password=password, url=url)


def _text_properties(client: Any, template_name: str) -> list[str]:
    """The template's property names of type ``text`` (the ones this use case writes).

    The ``title`` property is excluded: it is the row's title, not a metadata
    property the review table updates.
    """
    from uwazi_api.domain.property_schema import PropertyType

    template = client.templates.get_by_name(template_name)
    if template is None:
        raise RuntimeError(f"Template {template_name!r} does not exist on the instance")
    props = list(template.properties) + list(template.common_properties)
    return [p.name for p in props if p.type == PropertyType.TEXT and p.name != "title"]


def _row(client: Any, shared_id: str, language: str) -> dict[str, Any] | None:
    """The live raw of one (entity, language) ROW, or None when the row is absent.

    This is the same read the agent's own repository does
    (``GET /api/entities?sharedId=…`` with the ``locale`` cookie), so what the
    driver sees is what Uwazi stores.
    """
    response = client.http.request_adapter.get(
        url=f"{client.http.url}/api/entities",
        headers=client.http.headers,
        cookies={"locale": language},
        params={"sharedId": shared_id, "omitRelationships": "true"},
    )
    if response.status_code != 200:
        return None
    rows = response.json().get("rows", [])
    return rows[0] if rows else None


def _text_value(raw: dict[str, Any], property_name: str) -> str | None:
    """The property's stored value on one row, as the plain string Uwazi holds.

    A text property is stored as a list of ``{"value": …}`` entries (at most
    one per row). ``None`` means the property is unset on that row.
    """
    cell = (raw.get("metadata") or {}).get(property_name)
    if not cell:
        return None
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        for entry in cell:
            if isinstance(entry, dict) and "value" in entry:
                value = entry["value"]
                return value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            if isinstance(entry, str):
                return entry
    return None


def _comparable(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A raw reduced to the fields a comparison can judge.

    ``editDate`` is server-managed (it advances on every save, including the
    revert's own save), so it is excluded — exactly like the agent's own
    post-revert verification does. ``relations`` is a read-only denormalized
    view re-derived on every read, so it is excluded too. Everything else is
    data: a difference here is a REAL difference.
    """
    if raw is None:
        return {}
    return {k: v for k, v in raw.items() if k not in ("editDate", "relations")}


def _metadata_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The metadata PROPERTY names that differ between two raws."""
    bm = before.get("metadata") if isinstance(before.get("metadata"), dict) else {}
    am = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
    return sorted(key for key in set(bm) | set(am) if bm.get(key) != am.get(key))


def _field_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The NON-metadata top-level fields that differ between two raws.

    The metadata is compared property by property (see :func:`_metadata_diff`),
    so it is excluded here: this covers the rest of the entity — the title,
    the template, the files, the permissions.
    """
    return sorted(key for key in set(before) | set(after) if key != "metadata" and before.get(key) != after.get(key))


def _pick_targets(client: Any, template_name: str, count: int) -> list[str]:
    """The sharedIds of ``count`` live entities that have a row in EVERY language.

    The use case writes the same property in two languages, so an entity that
    has no ``es`` row cannot carry it. The pick is deterministic (the instance's
    own search order) so a re-run starts from the same place.
    """
    en_rows = client.search.get(batch_size=200, template_name=template_name, language="en")
    es_rows = client.search.get(batch_size=200, template_name=template_name, language="es")
    es_ids = {row.shared_id for row in es_rows if row.shared_id}
    picked: list[str] = []
    for row in en_rows:
        if not row.shared_id or row.shared_id not in es_ids:
            continue
        picked.append(row.shared_id)
        if len(picked) >= count:
            break
    if len(picked) < count:
        raise RuntimeError(
            f"only {len(picked)} entities of template {template_name!r} have both an 'en' and an 'es' row; "
            f"{count} are needed"
        )
    return picked


# --- the driver's own ids -----------------------------------------------------


def _run_id(task_no: int, suffix: str = "") -> str:
    """A fresh run id for one task step (never collides with an existing run)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"-{suffix}" if suffix else ""
    return f"tpe2e-task{task_no}{tag}-{stamp}-{uuid.uuid4().hex[:6]}"


def _task_id(task_no: int) -> str:
    """The task id (also the task YAML's filename under ``TASKS_PATH``)."""
    return f"tpe2e-task-{task_no}"


def _clear_previous(run_id: str, task_id: str) -> None:
    """Remove a leftover run folder, its review rows and its task file."""
    from uwazi_admin_agent.drivers.web.run_service import delete_run

    try:
        delete_run(run_id)
    except FileNotFoundError:
        pass
    prompt_file = PROMPTS_PATH / f"{run_id}.yaml"
    if prompt_file.is_file():
        prompt_file.unlink()
    task_file = TASKS_PATH / f"{task_id}.yaml"
    if task_file.is_file():
        task_file.unlink()


# --- the task prompts ---------------------------------------------------------


def _prompt_single_language(
    template_name: str,
    property_name: str,
    shared_ids: list[str],
    language: str,
    value: str,
) -> str:
    """The operator prompt for a step that writes ONE language.

    It names the template, the property, the exact entities and the language,
    so the generated script's target set is unambiguous. The driver passes the
    entity ids on purpose: the USE CASE is a bounded batch, and a prompt that
    named no target would make the LLM scan the whole template (5018 documents).
    """
    ids = ", ".join(shared_ids)
    return (
        f"Under template '{template_name}', set the text property '{property_name}' to the value "
        f"'{value}' for the following {len(shared_ids)} entities, in the '{language}' language only: "
        f"{ids}. Update ONLY the '{language}' row of each of those entities and leave every other "
        f"property and every other language untouched. Set result to a short summary of what you changed."
    )


def _prompt_both_languages(
    template_name: str,
    property_name: str,
    shared_ids: list[str],
    value_en: str,
    value_es: str,
) -> str:
    """The operator prompt for a step that writes BOTH languages in one run."""
    return (
        f"Under template '{template_name}', set the text property '{property_name}' to a DIFFERENT value per "
        f"language for the following {len(shared_ids)} entities: the 'en' row gets '{value_en}' and the 'es' row "
        f"gets '{value_es}'. Entities: {', '.join(shared_ids)}. Write the 'en' value with language 'en' and the "
        f"'es' value with language 'es' — they are two rows of the same entity, so they must not be merged. "
        f"Update no other property. Set result to a short summary of what you changed."
    )


def _prompt_mixed_batches(
    template_name: str,
    property_name: str,
    shared_ids_en: list[str],
    shared_ids_es: list[str],
    value_en: str,
    value_es: str,
) -> str:
    """The operator prompt for a step that writes a DIFFERENT batch size per language."""
    return (
        f"Under template '{template_name}', set the text property '{property_name}' on the following entities, "
        f"with a different value per language. In the 'en' language set it to '{value_en}' for these "
        f"{len(shared_ids_en)} entities: {', '.join(shared_ids_en)}. In the 'es' language set it to '{value_es}' "
        f"for these {len(shared_ids_es)} entities: {', '.join(shared_ids_es)}. Write each language's row with "
        f"its own language argument. Update no other property. Set result to a short summary of what you changed."
    )


# --- the flow -----------------------------------------------------------------


class _Runner:
    """The live flow: create → plan → run → revert → verify, plus the checks."""

    def __init__(self, user: str, password: str, client: Any) -> None:
        self.user = user
        self.password = password
        self._client = client

    # --- steps ---------------------------------------------------------------

    async def create(self, run_id: str, prompt: str) -> None:
        """The wizard's "Generate": persist the prompt, generate, plan."""
        await create_and_generate(run_id, prompt, self.user, self.password)

    def planned_rows(self, run_id: str) -> list[tuple[str, str | None, str | None]]:
        """The review table's (sharedId, language, property) rows of the current attempt.

        Read through the SAME store the review page reads, so what the driver
        checks is what the operator saw.
        """
        attempt = current_attempt(run_id)
        if attempt is None:
            return []
        store = build_suggestion_store()
        rows, _total = store.query(run_id, attempt.attempt, SuggestionFilter(limit=1000))
        return [(row.shared_id, row.language, row.property_name) for row in rows]

    async def run_rows(self, run_id: str, selection: list[tuple[str, str | None]]) -> None:
        """The review page's "Run": push the checked (sharedId, language) rows."""
        await apply_outcomes(run_id, self.user, self.password, selection)

    def manifest_status(self, run_id: str) -> RunStatus | None:
        """The run's manifest status (the runs table's Status column)."""
        from uwazi_admin_agent.drivers.web.run_service import get_run

        try:
            return get_run(run_id).status
        except FileNotFoundError:
            return None

    # --- reads ---------------------------------------------------------------

    def live_value(self, shared_id: str, language: str, property_name: str) -> str | None:
        """The property's CURRENT value on one live language row."""
        raw = _row(self._client, shared_id, language)
        return None if raw is None else _text_value(raw, property_name)

    def live_title(self, shared_id: str, language: str) -> str | None:
        """The row's current title (a text property too, but not the one under test)."""
        raw = _row(self._client, shared_id, language)
        return None if raw is None else raw.get("title")

    def live_raw(self, shared_id: str, language: str) -> dict[str, Any]:
        """The row's CURRENT comparable raw (the whole entity, minus the
        server-managed fields)."""
        return _comparable(_row(self._client, shared_id, language))


def _task_use_case() -> TaskUseCase:
    """The task layer over the SAME single-run functions the web UI uses.

    The task layer is pure orchestration, so it is handed the very same
    revert/create functions the single-run flow uses — one code path for the
    actual work, no second implementation to keep in sync.
    """
    from uwazi_admin_agent.adapters.tasks_store import YamlTaskStore

    return TaskUseCase(
        task_store=YamlTaskStore(TASKS_PATH),
        backup_store=build_backup_store(),
        revert_run=revert_run,
        create_run=create_and_generate,
    )


# --- one step -----------------------------------------------------------------


async def _run_step(
    runner: _Runner,
    step: StepReport,
    selection: list[tuple[str, str | None]],
    expect: dict[tuple[str, str], str | None],
    property_name: str,
) -> None:
    """Run one task step end to end and record the checks.

    The order is the UI's: generate → plan → Run → check → (the task's revert
    happens once, after every step) → check.

    The "untouched" baseline is captured HERE, right before the Run — not
    before the task started. A task's later step runs on top of the earlier
    step's writes, so only the state at the step's own start is the correct
    reference for "the rows this step did not select are unchanged".
    """
    try:
        await runner.create(step.run_id, step.prompt)
    except Exception as exc:  # noqa: BLE001 — the report carries the failure
        step.error = f"generation failed: {exc}"
        logger.exception("step {}: generation failed", step.run_id)
        return

    planned_pairs = {(sid, lang) for sid, lang, _prop in runner.planned_rows(step.run_id)}
    step.add(
        "planning pass recorded a row per (entity, language)",
        bool(planned_pairs),
        f"planned pairs: {sorted(planned_pairs)}",
    )
    step.add(
        "planning pass recorded exactly the requested entity/language pairs",
        set(selection).issubset(planned_pairs),
        f"missing: {sorted(set(selection) - planned_pairs)}",
    )

    selected = set(selection)
    untouched = {
        (sid, lang): runner.live_value(sid, lang, property_name) for sid, lang in step.pairs if (sid, lang) not in selected
    }
    # The WHOLE entity of every selected row, captured before the run: the
    # check "the run changed nothing but the target property" needs every
    # other property's value, not just the target's.
    before_raws = {pair: runner.live_raw(*pair) for pair in selected}
    untouched_raws = {pair: runner.live_raw(*pair) for pair in untouched}

    try:
        await runner.run_rows(step.run_id, selection)
    except Exception as exc:  # noqa: BLE001 — the report carries the failure
        step.error = f"run failed: {exc}"
        logger.exception("step {}: run failed", step.run_id)
        return

    step.add(
        "the run wrote the expected value on every selected row",
        all(runner.live_value(sid, lang, prop) == value for (sid, lang), (prop, value) in expect.items()),
        f"expected {sorted({value for _prop, value in expect.values()})}",
    )
    step.add(
        "the run left every row it did not select untouched",
        all(runner.live_value(sid, lang, property_name) == value for (sid, lang), value in untouched.items()),
        f"{len(untouched)} row(s) must still hold their original value",
    )

    # --- data preservation: NOTHING but the target property may change ------
    lost: dict[tuple[str, str], list[str]] = {}
    for pair, before_raw in before_raws.items():
        after_raw = runner.live_raw(*pair)
        changed = _metadata_diff(before_raw, after_raw)
        if changed != [property_name]:
            lost[pair] = changed
    step.add(
        "the run changed ONLY the target property (no other property lost)",
        not lost,
        f"changed properties: {lost}" if lost else "every other property is intact",
    )

    top_level_lost: dict[tuple[str, str], list[str]] = {}
    for pair, before_raw in before_raws.items():
        changed = _field_diff(before_raw, runner.live_raw(*pair))
        if changed:
            top_level_lost[pair] = changed
    step.add(
        "the run changed no other field of the entity (title, files, ...)",
        not top_level_lost,
        f"changed fields: {top_level_lost}" if top_level_lost else "the rest of the entity is byte-identical",
    )

    untouched_lost = {
        pair: _metadata_diff(before_raw, runner.live_raw(*pair))
        for pair, before_raw in untouched_raws.items()
        if _metadata_diff(before_raw, runner.live_raw(*pair))
    }
    step.add(
        "the run changed NOTHING on the rows it did not select",
        not untouched_lost,
        f"changed: {untouched_lost}" if untouched_lost else "the sibling rows are byte-identical",
    )
    step.add(
        "the run's manifest is EXECUTED",
        runner.manifest_status(step.run_id) == RunStatus.EXECUTED,
        f"status={runner.manifest_status(step.run_id)}",
    )


# --- the three tasks ----------------------------------------------------------


async def _task_two_steps(
    runner: _Runner,
    task_no: int,
    template_name: str,
    property_name: str,
    shared_ids: list[str],
    value_en: str,
    value_es: str,
) -> TaskReport:
    """TASK 1: TWO steps — one per language, over the SAME entities.

    The multistep-task shape the UI is built around: the task's "Revert task"
    button undoes step 2 first, then step 1.
    """
    task_id = _task_id(task_no)
    report = TaskReport(task_id=task_id, name=f"text-property-e2e task {task_no}: one step per language")
    baseline = {(sid, lang): runner.live_value(sid, lang, property_name) for sid in shared_ids for lang in LANGUAGES}
    baseline_raws = {pair: runner.live_raw(*pair) for pair in baseline}

    steps = [
        StepReport(
            title=f"write the 'en' rows of {len(shared_ids)} entities",
            run_id=_run_id(task_no, "en"),
            prompt=_prompt_single_language(template_name, property_name, shared_ids, "en", value_en),
            pairs=[(sid, "en") for sid in shared_ids],
        ),
        StepReport(
            title=f"write the 'es' rows of the same {len(shared_ids)} entities",
            run_id=_run_id(task_no, "es"),
            prompt=_prompt_single_language(template_name, property_name, shared_ids, "es", value_es),
            pairs=[(sid, "es") for sid in shared_ids],
        ),
    ]
    report.steps = steps

    use_case = _task_use_case()
    await use_case.create_task(
        task_id,
        report.name,
        [(step.run_id, step.title, step.prompt) for step in steps],
        user=runner.user,
        password=runner.password,
    )

    await _run_step(
        runner,
        steps[0],
        [(sid, "en") for sid in shared_ids],
        {(sid, "en"): (property_name, value_en) for sid in shared_ids},
        property_name,
    )
    await _run_step(
        runner,
        steps[1],
        [(sid, "es") for sid in shared_ids],
        {(sid, "es"): (property_name, value_es) for sid in shared_ids},
        property_name,
    )

    report.add(
        "the task's step count is 2 (one per language)",
        len(use_case.get_task(task_id).steps) == 2,
        f"steps={len(use_case.get_task(task_id).steps)}",
    )
    await _revert_task(runner, report, baseline, property_name, baseline_raws)
    return report


async def _task_one_step_both_languages(
    runner: _Runner,
    task_no: int,
    template_name: str,
    property_name: str,
    shared_ids: list[str],
    value_en: str,
    value_es: str,
) -> TaskReport:
    """TASK 2: ONE step covering BOTH languages of every entity.

    A single script that updates the ``en`` row and the ``es`` row of the same
    entities in one pass. The review table then carries two rows per entity and
    the Run pushes both.
    """
    task_id = _task_id(task_no)
    report = TaskReport(task_id=task_id, name=f"text-property-e2e task {task_no}: one run, both languages")
    baseline = {(sid, lang): runner.live_value(sid, lang, property_name) for sid in shared_ids for lang in LANGUAGES}
    baseline_raws = {pair: runner.live_raw(*pair) for pair in baseline}

    step = StepReport(
        title=f"write the 'en' AND the 'es' row of {len(shared_ids)} entities in one run",
        run_id=_run_id(task_no),
        prompt=_prompt_both_languages(template_name, property_name, shared_ids, value_en, value_es),
        pairs=[(sid, lang) for sid in shared_ids for lang in LANGUAGES],
    )
    report.steps = [step]

    use_case = _task_use_case()
    await use_case.create_task(
        task_id,
        report.name,
        [(step.run_id, step.title, step.prompt)],
        user=runner.user,
        password=runner.password,
    )

    await _run_step(
        runner,
        step,
        [(sid, lang) for sid in shared_ids for lang in LANGUAGES],
        {
            **{(sid, "en"): (property_name, value_en) for sid in shared_ids},
            **{(sid, "es"): (property_name, value_es) for sid in shared_ids},
        },
        property_name,
    )
    step.add(
        "no row holds the OTHER language's value",
        all(runner.live_value(sid, "en", property_name) != value_es for sid in shared_ids)
        and all(runner.live_value(sid, "es", property_name) != value_en for sid in shared_ids),
        "the two languages must not have been swapped",
    )
    report.add(
        "the task's step count is 1",
        len(use_case.get_task(task_id).steps) == 1,
        f"steps={len(use_case.get_task(task_id).steps)}",
    )
    await _revert_task(runner, report, baseline, property_name, baseline_raws)
    return report


async def _task_mixed_batches(
    runner: _Runner,
    task_no: int,
    template_name: str,
    property_name: str,
    shared_ids_en: list[str],
    shared_ids_es: list[str],
    value_en: str,
    value_es: str,
) -> TaskReport:
    """TASK 3: a DIFFERENT batch size per language (2 in one, 4 in the other).

    Proves the language groups are independent in COUNT too: writing 4 ``es``
    rows must not touch the 2 ``en`` rows, and the revert must restore all 6.
    """
    task_id = _task_id(task_no)
    report = TaskReport(task_id=task_id, name=f"text-property-e2e task {task_no}: a different batch size per language")
    all_ids = list(dict.fromkeys([*shared_ids_en, *shared_ids_es]))
    baseline = {(sid, lang): runner.live_value(sid, lang, property_name) for sid in all_ids for lang in LANGUAGES}
    baseline_raws = {pair: runner.live_raw(*pair) for pair in baseline}

    step = StepReport(
        title=f"write {len(shared_ids_en)} 'en' rows and {len(shared_ids_es)} 'es' rows in one run",
        run_id=_run_id(task_no),
        prompt=_prompt_mixed_batches(template_name, property_name, shared_ids_en, shared_ids_es, value_en, value_es),
        pairs=[(sid, "en") for sid in shared_ids_en] + [(sid, "es") for sid in shared_ids_es],
    )
    report.steps = [step]

    use_case = _task_use_case()
    await use_case.create_task(
        task_id,
        report.name,
        [(step.run_id, step.title, step.prompt)],
        user=runner.user,
        password=runner.password,
    )

    await _run_step(
        runner,
        step,
        [(sid, "en") for sid in shared_ids_en] + [(sid, "es") for sid in shared_ids_es],
        {
            **{(sid, "en"): (property_name, value_en) for sid in shared_ids_en},
            **{(sid, "es"): (property_name, value_es) for sid in shared_ids_es},
        },
        property_name,
    )
    report.add(
        "the task's step count is 1",
        len(use_case.get_task(task_id).steps) == 1,
        f"steps={len(use_case.get_task(task_id).steps)}",
    )
    await _revert_task(runner, report, baseline, property_name, baseline_raws)
    return report


# --- the driver ---------------------------------------------------------------


def _restore_property(
    client: Any,
    shared_ids: list[str],
    property_name: str,
    before: dict[tuple[str, str], str | None],
) -> list[str]:
    """Put every (entity, language) row back to its captured value.

    The revert path is the ONE undo; this is the safety net for a run whose
    revert failed or never happened (a crashed generation, a refused write). It
    writes through the same endpoint the agent writes to
    (``POST /api/entities`` with the ``locale`` cookie) and is therefore a
    faithful restore, not a re-run of the task.
    """
    problems: list[str] = []
    for (shared_id, language), value in before.items():
        try:
            raw = _row(client, shared_id, language)
            if raw is None:
                problems.append(f"{shared_id}/{language}: row absent")
                continue
            if _text_value(raw, property_name) == value:
                continue
            metadata = dict(raw.get("metadata") or {})
            metadata[property_name] = [] if value is None else [{"value": value}]
            payload = {
                # ``_id`` is REQUIRED by Uwazi's entity write: the payload
                # addresses the existing row (an update), it does not create one.
                "_id": raw.get("_id"),
                "sharedId": shared_id,
                "title": raw.get("title"),
                "template": raw.get("template"),
                "language": language,
                "metadata": metadata,
            }
            response = client.http.post_json(
                url=f"{client.http.url}/api/entities",
                json={k: v for k, v in payload.items() if v is not None},
                cookies={"locale": language},
            )
            if response.status_code != 200:
                problems.append(f"{shared_id}/{language}: HTTP {response.status_code}")
        except Exception as exc:  # noqa: BLE001 — one row's failure must not stop the rest
            problems.append(f"{shared_id}/{language}: {exc}")
    return problems


async def _revert_task(
    runner: _Runner,
    report: TaskReport,
    baseline: dict[tuple[str, str], str | None],
    property_name: str,
    baseline_raws: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> None:
    """The task page's "Revert task": undo every executed step, latest first.

    The revert plan comes from the task layer's own gate, so the driver
    exercises the same ordering the UI does. ``baseline`` is the state the task
    started from — the task's own first-touch snapshot, so a correct revert
    lands back exactly there.

    ``baseline_raws`` holds each row's WHOLE raw from before the task, so the
    check can assert the revert restored the ENTIRE entity — every property,
    not just the one under test.
    """
    use_case = _task_use_case()
    try:
        view = use_case.get_task(report.task_id)
        report.add(
            "the task's revert plan names every executed step",
            bool(view.revert_targets),
            f"plan={view.revert_targets}",
        )
        await use_case.revert_task(report.task_id, runner.user, runner.password)
        for step in report.steps:
            step.add(
                "the task's revert restored this step's rows",
                all(runner.live_value(sid, lang, property_name) == baseline[(sid, lang)] for sid, lang in step.pairs),
                "every row must hold its pre-task value again",
            )
            if baseline_raws is not None:
                not_restored = {
                    pair: _metadata_diff(before_raw, runner.live_raw(*pair))
                    for pair, before_raw in baseline_raws.items()
                    if _metadata_diff(before_raw, runner.live_raw(*pair))
                }
                step.add(
                    "the task's revert restored the WHOLE entity (every property)",
                    not not_restored,
                    (
                        f"still different: {not_restored}"
                        if not_restored
                        else "the entity is byte-identical to its pre-task state"
                    ),
                )
                fields_lost = {
                    pair: _field_diff(before_raw, runner.live_raw(*pair)) for pair, before_raw in baseline_raws.items()
                }
                fields_lost = {pair: fields for pair, fields in fields_lost.items() if fields}
                step.add(
                    "the task's revert restored every other field (title, files, ...)",
                    not fields_lost,
                    f"still different: {fields_lost}" if fields_lost else "no field is left changed",
                )
            step.add(
                "the step's run is REVERTED",
                runner.manifest_status(step.run_id) == RunStatus.REVERTED,
                f"status={runner.manifest_status(step.run_id)}",
            )
    except Exception as exc:  # noqa: BLE001 — the report carries the failure
        report.error = report.error or f"revert failed: {exc}"
        logger.exception("task {}: revert failed", report.task_id)


def _active_run_snapshot() -> str | None:
    """The current ``active_run.yaml`` content (restored at the end)."""
    if not RUNS_FILE.is_file():
        return None
    return RUNS_FILE.read_text(encoding="utf-8")


def _restore_active_run(content: str | None) -> None:
    """Put ``active_run.yaml`` back (the driver must not move the operator's pointer)."""
    if content is None:
        if RUNS_FILE.is_file():
            RUNS_FILE.unlink()
        return
    RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
    RUNS_FILE.write_text(content, encoding="utf-8")


async def run_text_property_e2e(
    user: str,
    password: str,
    template_name: str = DEFAULT_TEMPLATE,
    property_name: str = "",
    entities: int = DEFAULT_ENTITIES_PER_LANGUAGE,
    keep_runs: bool = False,
) -> int:
    """Run the three tasks against the live instance; return a process exit code.

    ``property_name`` defaults to the template's first non-title ``text``
    property, so the driver follows the INSTANCE's schema instead of a
    hard-coded one.
    """
    client = _client(user, password)
    templates = [t.name for t in client.templates.get()]
    if template_name not in templates:
        print(f"error: template {template_name!r} is not on the instance (have: {', '.join(templates)})", flush=True)
        return 2

    text_props = _text_properties(client, template_name)
    if not text_props:
        print(f"error: template {template_name!r} has no property of type 'text' to exercise", flush=True)
        return 2
    resolved_property = property_name or text_props[0]
    if property_name and property_name not in text_props:
        print(
            f"error: {property_name!r} is not a 'text' property of {template_name!r} (have: {', '.join(text_props)})",
            flush=True,
        )
        return 2

    # The three tasks need 2 + 2 + (2 + 4) entities, and the batches must not
    # overlap in a way that hides a leak, so the pick is generous.
    needed = max(entities * 2, entities + 4, 6)
    try:
        pool = _pick_targets(client, template_name, needed)
    except RuntimeError as exc:
        print(f"error: {exc}", flush=True)
        return 2

    print(f"instance: {os.environ.get('UWAZI_URL')} (user {user})", flush=True)
    print(f"template: {template_name!r}  text property: {resolved_property!r}", flush=True)
    print(f"target entities: {len(pool)} ({', '.join(pool)})", flush=True)
    print("", flush=True)

    runner = _Runner(user, password, client)

    # The values the tasks write. They are deliberately DIFFERENT per language
    # AND per task, so a value that leaks across languages or across tasks is
    # visible in the live data.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    values = {task_no: (f"E2E-EN-T{task_no}-{stamp}", f"E2E-ES-T{task_no}-{stamp}") for task_no in (1, 2, 3)}

    baseline = {(sid, lang): runner.live_value(sid, lang, resolved_property) for sid in pool for lang in LANGUAGES}
    titles = {(sid, lang): runner.live_title(sid, lang) for sid in pool for lang in LANGUAGES}
    # The WHOLE entity of every target row: the idempotency statement is about
    # the ENTIRE row, not just the property under test.
    baseline_raws = {pair: runner.live_raw(*pair) for pair in baseline}

    active_run_before = _active_run_snapshot()
    reports: list[TaskReport] = []
    restore_problems: list[str] = []
    try:
        # TASK 1 — two steps, one per language, over the SAME entities.
        reports.append(await _task_two_steps(runner, 1, template_name, resolved_property, pool[:entities], *values[1]))
        # TASK 2 — one step covering BOTH languages of every entity.
        reports.append(
            await _task_one_step_both_languages(runner, 2, template_name, resolved_property, pool[:entities], *values[2])
        )
        # TASK 3 — a different batch size per language.
        reports.append(
            await _task_mixed_batches(runner, 3, template_name, resolved_property, pool[:2], pool[2 : 2 + 4], *values[3])
        )
    finally:
        # The safety net: whatever the tasks left behind is put back, so the
        # driver is idempotent even when a task failed mid-way.
        restore_problems = _restore_property(client, pool, resolved_property, baseline)
        _restore_active_run(active_run_before)

    # --- the report ---------------------------------------------------------
    print("", flush=True)
    failures = 0
    for report in reports:
        state = "OK" if report.ok else "FAILED"
        print(f"{report.task_id} [{state}] {report.name}", flush=True)
        if report.error:
            print(f"  [FAIL] {report.error}", flush=True)
            failures += 1
        for check in report.checks:
            print(check.line(), flush=True)
            if not check.ok:
                failures += 1
        for step in report.steps:
            print(f"  step {step.title} (run {step.run_id})", flush=True)
            if step.error:
                print(f"    [FAIL] {step.error}", flush=True)
                failures += 1
            for check in step.checks:
                print(f"  {check.line()}", flush=True)
                if not check.ok:
                    failures += 1
        print("", flush=True)

    # The idempotency statement: the instance is back where it started.
    after = {(sid, lang): runner.live_value(sid, lang, resolved_property) for sid in pool for lang in LANGUAGES}
    unchanged = after == baseline
    titles_after = {(sid, lang): runner.live_title(sid, lang) for sid in pool for lang in LANGUAGES}
    # The whole-entity statement: EVERY field of EVERY target row is back.
    whole_entity_drift = {
        pair: _metadata_diff(before_raw, runner.live_raw(*pair))
        for pair, before_raw in baseline_raws.items()
        if _metadata_diff(before_raw, runner.live_raw(*pair))
    }
    field_drift = {pair: _field_diff(before_raw, runner.live_raw(*pair)) for pair, before_raw in baseline_raws.items()}
    field_drift = {pair: fields for pair, fields in field_drift.items() if fields}
    print(
        f"idempotency: property values unchanged={unchanged}  titles unchanged={titles_after == titles}  "
        f"whole entities unchanged={not whole_entity_drift and not field_drift}",
        flush=True,
    )
    if restore_problems:
        print("restore problems (the revert did not cover these):", flush=True)
        for problem in restore_problems:
            print(f"  - {problem}", flush=True)
        failures += len(restore_problems)
    if not unchanged:
        diff = {key: (baseline[key], after[key]) for key in baseline if baseline[key] != after[key]}
        print(f"  property drift: {diff}", flush=True)
        failures += 1
    if whole_entity_drift or field_drift:
        print(f"  property drift: {whole_entity_drift}", flush=True)
        print(f"  field drift: {field_drift}", flush=True)
        failures += 1

    if not keep_runs:
        for report in reports:
            for step in report.steps:
                _clear_previous(step.run_id, report.task_id)

    print(f"\n{'PASS' if failures == 0 else 'FAIL'}: {failures} failing check(s)", flush=True)
    return 0 if failures == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """The CLI entry point (``uwazi-admin-agent text-property-e2e``)."""
    parser = argparse.ArgumentParser(
        prog="uwazi-admin-agent text-property-e2e",
        description="Exercise the per-language text-property update + revert path against the live instance.",
    )
    parser.add_argument("--user", default=os.environ.get("UWAZI_USER", "admin"), help="Uwazi user (default: admin).")
    parser.add_argument(
        "--password",
        default=os.environ.get("UWAZI_PASSWORD", "admin"),
        help="Uwazi password (default: admin).",
    )
    parser.add_argument("--template", default=DEFAULT_TEMPLATE, help=f"Template to exercise (default: {DEFAULT_TEMPLATE}).")
    parser.add_argument("--property", default="", help="Text property to write (default: the template's first one).")
    parser.add_argument(
        "--entities",
        type=int,
        default=DEFAULT_ENTITIES_PER_LANGUAGE,
        help=f"Entities per language for tasks 1 and 2 (default: {DEFAULT_ENTITIES_PER_LANGUAGE}).",
    )
    parser.add_argument(
        "--keep-runs",
        action="store_true",
        help="Keep the runs and task files on disk (default: delete them).",
    )
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    return asyncio.run(
        run_text_property_e2e(
            user=args.user,
            password=args.password,
            template_name=args.template,
            property_name=args.property,
            entities=max(2, args.entities),
            keep_runs=args.keep_runs,
        )
    )


__all__ = ["main", "run_text_property_e2e"]
