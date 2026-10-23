"""Shared support for the admin agent's live, service-level E2E tests.

The tests in this directory exercise the same service functions the web UI
calls (``run_auto_scan``, ``apply_outcomes``, ``revert_run``,
``revert_entities_selection``, ``update_table_from_uwazi`` and
``TaskUseCase``). They never click through NiceGUI.

Every test owns its data:

- one uniquely named template and all of its entities are created here;
- runs and tasks use a namespace derived from that template name;
- the finalizer removes the template's entities, the template, the task
  definitions, and the runs' backup/suggestion artifacts;
- the finalizer then asserts there is no namespaced residue.

The planning pass is deterministic: the test writes the same ``script.py`` the
generator would emit and runs the real automatic scan. No LLM output is needed,
so the suite is reproducible while still exercising the real script namespace,
backup intercept, suggestion store, apply heuristic, and revert machinery.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest
from dotenv import load_dotenv
from loguru import logger

from uwazi_admin_agent.configuration import RUNS_PATH, TASKS_PATH
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter
from uwazi_admin_agent.domain.task import TaskDef, TaskView
from uwazi_admin_agent.drivers.runtime import build_backup_store, build_file_cache
from uwazi_admin_agent.drivers.web.run_service import (
    apply_outcomes,
    build_suggestion_store,
    build_task_store,
    current_attempt,
    delete_run,
    get_table_update_time,
    preflight_selection,
    revert_entities_selection,
    revert_run,
    run_auto_scan,
    update_table_from_uwazi,
)
from uwazi_admin_agent.use_cases.task_use_case import TaskUseCase
from uwazi_api.client import UwaziClient
from uwazi_api.domain.entity import Entity
from uwazi_api.domain.property_schema import PropertySchema
from uwazi_api.domain.property_type import PropertyType
from uwazi_api.domain.template import Template
from uwazi_api.testing.e2e_guard import require_local_e2e_target

load_dotenv()

UWAZI_URL = os.getenv("UWAZI_URL", "http://localhost:3000")
UWAZI_USER = os.getenv("UWAZI_USER", "admin")
UWAZI_PASSWORD = os.getenv("UWAZI_PASSWORD", "admin")

DEFAULT_PROPERTY = "notes"
SECOND_PROPERTY = "summary"


# --- pure helpers -------------------------------------------------------------


def comparable(raw: dict[str, Any] | None) -> dict[str, Any]:
    """A raw reduced to the data fields a restore must reproduce.

    ``editDate`` is server-managed and ``relations`` is a read-only derived
    view, so neither belongs in an equality assertion. Everything else is data.
    """
    if raw is None:
        return {}
    return {key: value for key, value in raw.items() if key not in ("editDate", "relations")}


def text_value(raw: dict[str, Any] | None, property_name: str = DEFAULT_PROPERTY) -> str | None:
    """The plain string a text property holds on one entity row."""
    if raw is None:
        return None
    cell = (raw.get("metadata") or {}).get(property_name)
    if not cell:
        return None
    if isinstance(cell, str):
        return cell
    if isinstance(cell, list):
        for entry in cell:
            if isinstance(entry, dict) and "value" in entry:
                value = entry["value"]
                return value if isinstance(value, str) else str(value)
            if isinstance(entry, str):
                return entry
    return None


def metadata_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Metadata property names whose value differs between two raws."""
    before_meta = before.get("metadata") if isinstance(before.get("metadata"), dict) else {}
    after_meta = after.get("metadata") if isinstance(after.get("metadata"), dict) else {}
    return sorted(key for key in set(before_meta) | set(after_meta) if before_meta.get(key) != after_meta.get(key))


def field_diff(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Non-metadata top-level fields that differ between two raws."""
    return sorted(key for key in set(before) | set(after) if key != "metadata" and before.get(key) != after.get(key))


def _as_repr(value: Any) -> str:
    """A Python literal for a value embedded in a generated test script."""
    return repr(value)


def update_script(
    template_name: str,
    shared_ids: list[str],
    language: str,
    value: str,
    property_name: str = DEFAULT_PROPERTY,
) -> str:
    """A deterministic planning script that updates one language of the targets."""
    return (
        f"targets = {_as_repr(shared_ids)}\n"
        f"update_entities(\n"
        f"    [\n"
        f"        {{\n"
        f"            'shared_id': shared_id,\n"
        f"            'template_name': {_as_repr(template_name)},\n"
        f"            'language': {_as_repr(language)},\n"
        f"            'metadata': {{{_as_repr(property_name)}: {_as_repr(value)}}},\n"
        f"        }}\n"
        f"        for shared_id in targets\n"
        f"    ],\n"
        f"    language={_as_repr(language)},\n"
        f")\n"
        f"result = f'updated {{len(targets)}} row(s) in {language}'\n"
    )


def update_languages_script(
    template_name: str,
    shared_ids: list[str],
    values_by_language: dict[str, str],
    property_name: str = DEFAULT_PROPERTY,
) -> str:
    """A planning script that writes a distinct value per language."""
    blocks = "\n".join(
        update_script(template_name, shared_ids, language, value, property_name)
        for language, value in values_by_language.items()
    )
    return f"{blocks}result = 'updated every requested language'\n"


def update_template_script(
    template_name: str,
    title_prefix: str,
    language: str,
    value: str,
    property_name: str = DEFAULT_PROPERTY,
) -> str:
    """A planning script that discovers the template's entities and updates them.

    Unlike :func:`update_script`, a target that disappears from the template is
    not written any more, which is what lets the table refresh mark its old row
    obsolete.
    """
    return (
        f"rows = query_entities_full(mode='by_template', template_name={_as_repr(template_name)}, "
        f"language={_as_repr(language)})\n"
        f"targets = [\n"
        f"    row['shared_id']\n"
        f"    for row in rows\n"
        f"    if str(row.get('title', '')).startswith({_as_repr(title_prefix)})\n"
        f"]\n"
        f"update_entities(\n"
        f"    [\n"
        f"        {{\n"
        f"            'shared_id': shared_id,\n"
        f"            'template_name': {_as_repr(template_name)},\n"
        f"            'language': {_as_repr(language)},\n"
        f"            'metadata': {{{_as_repr(property_name)}: {_as_repr(value)}}},\n"
        f"        }}\n"
        f"        for shared_id in targets\n"
        f"    ],\n"
        f"    language={_as_repr(language)},\n"
        f")\n"
        f"result = f'updated {{len(targets)}} row(s) in {language}'\n"
    )


def create_entity_script(
    template_name: str,
    title: str,
    language: str,
    metadata: dict[str, str] | None = None,
) -> str:
    """A planning script that creates one entity."""
    return (
        f"created = create_entities(\n"
        f"    [\n"
        f"        {{\n"
        f"            'title': {_as_repr(title)},\n"
        f"            'template_name': {_as_repr(template_name)},\n"
        f"            'language': {_as_repr(language)},\n"
        f"            'metadata': {_as_repr(metadata or {})},\n"
        f"        }}\n"
        f"    ],\n"
        f"    language={_as_repr(language)},\n"
        f")\n"
        f"result = f'created {{len(created)}} entity'\n"
    )


def delete_entities_script(shared_ids: list[str]) -> str:
    """A planning script that deletes the targets."""
    return (
        f"targets = {_as_repr(shared_ids)}\n"
        f"deleted = delete_entities(targets)\n"
        f"result = f'deleted {{len(deleted)}} entity/entities'\n"
    )


# --- language profile ---------------------------------------------------------


@dataclass(frozen=True)
class LanguageProfile:
    """The languages this instance actually has installed."""

    keys: tuple[str, ...]
    default: str
    secondary: str | None

    @classmethod
    def from_client(cls, client: UwaziClient) -> LanguageProfile:
        languages = list(client.settings.get_languages())
        if not languages:
            raise AssertionError("the live instance reports no configured languages")
        keys = tuple(language.key for language in languages)
        default = next((language.key for language in languages if language.default), keys[0])
        secondary = next((key for key in keys if key != default), None)
        return cls(keys=keys, default=default, secondary=secondary)


# --- the workspace ------------------------------------------------------------


class AdminE2EWorkspace:
    """One isolated live E2E fixture: a template, its entities, runs, and tasks."""

    def __init__(self, request: pytest.FixtureRequest, entity_count: int = 2) -> None:
        # The guard is deliberately the first action, before any client is built.
        require_local_e2e_target(UWAZI_URL)
        self._cleanup_done = False
        request.addfinalizer(self.cleanup)

        self.client: UwaziClient | None = None
        self.template_id: str | None = None
        self.entity_ids: list[str] = []
        self.run_ids: list[str] = []
        self.task_ids: list[str] = []
        self.namespace = f"e2e_admin_{uuid.uuid4().hex[:10]}"
        self._run_seq = 0
        self._task_seq = 0
        self._scripts_by_run: dict[str, str] = {}

        self.client = UwaziClient(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
        self.languages = LanguageProfile.from_client(self.client)
        self._create_template()
        for index in range(entity_count):
            self.create_entity(index=index)

    @property
    def template_name(self) -> str:
        """The live template's name (also the script's ``template_name``)."""
        return self.namespace

    # --- setup ---------------------------------------------------------------

    def _create_template(self) -> None:
        assert self.client is not None
        template = Template(
            name=self.namespace,
            properties=[
                PropertySchema(name=DEFAULT_PROPERTY, label="Notes", type=PropertyType.TEXT),
                PropertySchema(name=SECOND_PROPERTY, label="Summary", type=PropertyType.TEXT),
            ],
        )
        created = self.client.templates.set(self.languages.default, template)
        self.template_id = created["_id"]

    def create_entity(self, index: int | None = None) -> str:
        """Create one entity under this workspace's template and wait for search."""
        assert self.client is not None
        assert self.template_id is not None
        suffix = self._run_seq if index is None else index
        title = f"{self.namespace}_entity_{suffix}"
        shared_id = self.client.entities.upload(
            Entity(
                title=title,
                template=self.template_id,
                metadata={DEFAULT_PROPERTY: f"original-{suffix}"},
            ),
            self.languages.default,
        )
        self.entity_ids.append(shared_id)
        self.wait_for_searchable([shared_id])
        for language in self.languages.keys:
            assert self.raw(shared_id, language) is not None, (
                f"entity {shared_id} has no {language!r} row; this instance's language rows are incomplete"
            )
        return shared_id

    # --- ids -----------------------------------------------------------------

    def next_run_id(self, label: str = "") -> str:
        self._run_seq += 1
        suffix = f"_{label}" if label else ""
        return f"{self.namespace}_run{self._run_seq}{suffix}"

    def next_task_id(self, label: str = "") -> str:
        self._task_seq += 1
        suffix = f"_{label}" if label else ""
        return f"{self.namespace}_task{self._task_seq}{suffix}"

    def value(self, label: str) -> str:
        return f"{self.namespace}_{label}"

    def require_secondary(self) -> str:
        """Skip clearly when the instance has only one language."""
        if self.languages.secondary is None:
            pytest.skip("the instance has only one language; cross-language E2E is not applicable")
        for shared_id in self.entity_ids:
            if self.raw(shared_id, self.languages.secondary) is None:
                pytest.skip(
                    f"entity {shared_id} has no {self.languages.secondary!r} row; "
                    "cross-language E2E is not applicable on this instance"
                )
        return self.languages.secondary

    # --- live reads ----------------------------------------------------------

    def raw(self, shared_id: str, language: str) -> dict[str, Any] | None:
        """One entity-language raw, read exactly as the agent's repository reads it."""
        assert self.client is not None
        response = self.client.http.request_adapter.get(
            url=f"{self.client.http.url}/api/entities",
            headers=self.client.http.headers,
            cookies={"locale": language},
            params={"sharedId": shared_id, "omitRelationships": "true"},
        )
        if response.status_code != 200:
            return None
        rows = response.json().get("rows", [])
        return rows[0] if rows else None

    def text(self, shared_id: str, language: str, property_name: str = DEFAULT_PROPERTY) -> str | None:
        return text_value(self.raw(shared_id, language), property_name)

    def pairs(self, shared_ids: list[str] | None = None, languages: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
        ids = self.entity_ids if shared_ids is None else shared_ids
        langs = self.languages.keys if languages is None else languages
        return [(shared_id, language) for shared_id in ids for language in langs]

    def baseline(self, pairs: list[tuple[str, str]] | None = None) -> dict[tuple[str, str], dict[str, Any]]:
        wanted = self.pairs() if pairs is None else pairs
        return {pair: comparable(self.raw(*pair)) for pair in wanted}

    def assert_restored(self, before: dict[tuple[str, str], dict[str, Any]]) -> None:
        """Assert every captured row is byte-identical except server-managed fields."""
        problems: list[str] = []
        for (shared_id, language), expected in before.items():
            actual = comparable(self.raw(shared_id, language))
            if actual != expected:
                problems.append(
                    f"{shared_id}/{language}: metadata diff={metadata_diff(expected, actual)}, "
                    f"field diff={field_diff(expected, actual)}"
                )
        assert not problems, "rows were not restored:\n" + "\n".join(problems)

    def assert_property(
        self,
        shared_id: str,
        language: str,
        expected: str | None,
        property_name: str = DEFAULT_PROPERTY,
    ) -> None:
        actual = self.text(shared_id, language, property_name)
        assert actual == expected, f"{shared_id}/{language} {property_name}: expected {expected!r}, got {actual!r}"

    # --- search freshness ----------------------------------------------------

    def _search_template(self, language: str) -> list[Entity]:
        assert self.client is not None
        return self.client.search.get(batch_size=1000, template_name=self.namespace, language=language)

    def wait_for_searchable(self, shared_ids: list[str], timeout: float = 15.0) -> None:
        """Wait until Uwazi's search index reports every entity in the default language.

        The planning scan reads through search, so a run seeded right after an
        upload can otherwise see an empty template (Elasticsearch refresh lag).
        """
        wanted = {shared_id for shared_id in shared_ids if shared_id}
        if not wanted:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = {row.shared_id for row in self._search_template(self.languages.default) if row.shared_id}
            if wanted <= found:
                return
            time.sleep(0.25)
        found = {row.shared_id for row in self._search_template(self.languages.default) if row.shared_id}
        raise AssertionError(f"entities never became searchable: missing={sorted(wanted - found)}")

    def wait_until_not_searchable(self, shared_ids: list[str], timeout: float = 15.0) -> None:
        """Wait until Uwazi's search index stops reporting the deleted entities."""
        wanted = {shared_id for shared_id in shared_ids if shared_id}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = {row.shared_id for row in self._search_template(self.languages.default) if row.shared_id}
            if not (wanted & found):
                return
            time.sleep(0.25)
        found = {row.shared_id for row in self._search_template(self.languages.default) if row.shared_id}
        raise AssertionError(f"deleted entities still appear in search: {sorted(wanted & found)}")

    def wait_for_edit_date_change(
        self,
        shared_id: str,
        old_edit_date: Any,
        language: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        """Wait until the search index exposes a write (the drift probe's signal)."""
        wanted_language = language or self.languages.default
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = [row for row in self._search_template(wanted_language) if row.shared_id == shared_id]
            if rows and rows[0].edit_date != old_edit_date:
                return
            time.sleep(0.25)
        raise AssertionError(f"entity {shared_id}/{wanted_language} never showed a new editDate")

    def invalidate_entity_cache(self, shared_ids: list[str]) -> None:
        """Drop the persistent raws after an out-of-band write (a real cache miss)."""
        cache = build_file_cache(UWAZI_URL)
        if cache is not None:
            cache.invalidate_entities(shared_ids)

    def external_update(
        self,
        shared_id: str,
        value: str,
        language: str | None = None,
        property_name: str = DEFAULT_PROPERTY,
    ) -> None:
        """Change a value directly in Uwazi, as a human editor would."""
        assert self.client is not None
        assert self.template_id is not None
        wanted_language = language or self.languages.default
        before = self.raw(shared_id, wanted_language) or {}
        self.client.entities.update_partially(
            Entity(
                sharedId=shared_id,
                template=self.template_id,
                metadata={property_name: value},
            ),
            wanted_language,
        )
        self.invalidate_entity_cache([shared_id])
        self.wait_for_edit_date_change(shared_id, before.get("editDate"), wanted_language)

    def external_delete(self, shared_id: str) -> None:
        """Delete an entity directly in Uwazi, as a human editor would."""
        assert self.client is not None
        self.client.entities.delete(shared_id)
        self.invalidate_entity_cache([shared_id])
        self.wait_until_not_searchable([shared_id])

    def external_create(self, suffix: str) -> str:
        """Create an entity directly in Uwazi and wait for the search index."""
        assert self.client is not None
        assert self.template_id is not None
        title = f"{self.namespace}_external_{suffix}"
        shared_id = self.client.entities.upload(
            Entity(
                title=title,
                template=self.template_id,
                metadata={DEFAULT_PROPERTY: f"external-original-{suffix}"},
            ),
            self.languages.default,
        )
        self.entity_ids.append(shared_id)
        self.wait_for_searchable([shared_id])
        return shared_id

    # --- runs ----------------------------------------------------------------

    async def seed_run_async(
        self,
        run_id: str,
        script: str,
        prompt: str = "deterministic E2E planning pass",
    ) -> int:
        """Persist the script + manifest and await the real automatic planning scan."""
        self.run_ids.append(run_id)
        run_path = RUNS_PATH / run_id
        run_path.mkdir(parents=True, exist_ok=False)
        script_path = run_path / "script.py"
        script_path.write_text(script, encoding="utf-8")
        manifest = MigrationManifest(
            run_id=run_id,
            created_at=datetime.now(timezone.utc),
            prompt=prompt,
            script=script,
            status=RunStatus.PLANNED,
            snapshot_dir=str(run_path),
        )
        build_backup_store().save_manifest(run_id, manifest)
        return await run_auto_scan(run_id, UWAZI_USER, UWAZI_PASSWORD)

    def seed_run(self, run_id: str, script: str, prompt: str = "deterministic E2E planning pass") -> int:
        """Synchronous wrapper for standalone (non-task) seeded runs."""
        return asyncio.run(self.seed_run_async(run_id, script, prompt))

    def new_run(self, script: str, prompt: str = "deterministic E2E planning pass") -> str:
        run_id = self.next_run_id()
        self.seed_run(run_id, script, prompt)
        return run_id

    def manifest(self, run_id: str) -> MigrationManifest:
        return build_backup_store().load_manifest(run_id)

    def status(self, run_id: str) -> RunStatus:
        return self.manifest(run_id).status

    def attempt_number(self, run_id: str) -> int:
        attempt = current_attempt(run_id)
        assert attempt is not None, f"run {run_id} has no prediction attempt"
        return attempt.attempt

    def suggestions(
        self,
        run_id: str,
        *,
        include_obsolete: bool = False,
        property_name: str | None = None,
        language: str | None = None,
    ) -> list[Suggestion]:
        rows, _total = build_suggestion_store().query(
            run_id,
            self.attempt_number(run_id),
            SuggestionFilter(limit=1000, include_obsolete=include_obsolete),
        )
        if property_name is not None:
            rows = [row for row in rows if row.property_name == property_name]
        if language is not None:
            rows = [row for row in rows if row.language == language]
        return rows

    def selection(self, run_id: str, rows: list[Suggestion] | None = None) -> list[tuple[str, str | None]]:
        chosen = self.suggestions(run_id) if rows is None else rows
        return [(row.shared_id, row.language) for row in chosen]

    def apply(self, run_id: str, rows: list[Suggestion] | None = None, row_ids: list[int] | None = None) -> list[str]:
        selection = self.selection(run_id, rows)
        assert selection, f"run {run_id} has no runnable rows"
        return asyncio.run(apply_outcomes(run_id, UWAZI_USER, UWAZI_PASSWORD, selection, row_ids))

    def revert(self, run_id: str) -> None:
        asyncio.run(revert_run(run_id, UWAZI_USER, UWAZI_PASSWORD))

    def revert_selection(self, run_id: str, selection: list[tuple[str, str | None]]) -> Any:
        return asyncio.run(revert_entities_selection(run_id, selection, UWAZI_USER, UWAZI_PASSWORD))

    def update_table(self, run_id: str) -> Any:
        return asyncio.run(update_table_from_uwazi(run_id, UWAZI_USER, UWAZI_PASSWORD))

    def preflight(self, run_id: str, rows: list[Suggestion] | None = None) -> Any:
        chosen = self.suggestions(run_id) if rows is None else rows
        ids = [row.id for row in chosen if row.id is not None]
        return preflight_selection(run_id, ids, UWAZI_USER, UWAZI_PASSWORD)

    def table_update_time(self, run_id: str) -> datetime | None:
        return get_table_update_time(run_id)

    # --- tasks ---------------------------------------------------------------

    async def _seeded_create_run(self, run_id: str, prompt: str, user: str, password: str) -> None:
        del user, password  # the workspace already carries the instance credentials
        script = self._scripts_by_run.get(run_id)
        if script is None:
            raise AssertionError(f"no deterministic script registered for task step run {run_id!r}")
        await self.seed_run_async(run_id, script, prompt)

    def _task_use_case(self) -> TaskUseCase:
        return TaskUseCase(
            task_store=build_task_store(),
            backup_store=build_backup_store(),
            revert_run=revert_run,
            create_run=self._seeded_create_run,
        )

    def create_task(
        self,
        task_id: str,
        name: str,
        steps: list[tuple[str, str, str]],
        scripts: dict[str, str],
    ) -> TaskDef:
        """Create a task whose step runs are seeded deterministically, then scanned."""
        self.task_ids.append(task_id)
        self._scripts_by_run.update(scripts)
        return asyncio.run(
            self._task_use_case().create_task(
                task_id,
                name,
                steps,
                user=UWAZI_USER,
                password=UWAZI_PASSWORD,
            )
        )

    def add_existing_run(self, task_id: str, run_id: str, title: str, description: str = "") -> TaskDef:
        return self._task_use_case().add_existing_run(task_id, run_id, title, description)

    def task_view(self, task_id: str) -> TaskView:
        return self._task_use_case().get_task(task_id)

    def revert_task(self, task_id: str) -> list[str]:
        return asyncio.run(self._task_use_case().revert_task(task_id, UWAZI_USER, UWAZI_PASSWORD))

    # --- cleanup -------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove every artifact this workspace created, then assert no residue."""
        if self._cleanup_done:
            return
        self._cleanup_done = True
        problems: list[str] = []

        if self.client is not None and self.template_id is not None:
            known_entities = self._known_entity_ids()
            self._delete_template_entities(problems)
            self._delete_template(problems)
            for shared_id in sorted(known_entities):
                for language in self.languages.keys:
                    if self.raw(shared_id, language) is not None:
                        problems.append(f"entity still readable: {shared_id}/{language}")

        for task_id in dict.fromkeys(self.task_ids):
            try:
                build_task_store().delete_task(task_id)
            except Exception as exc:  # noqa: BLE001 - report, never abort remaining cleanup
                problems.append(f"task {task_id}: {exc!r}")
            if (TASKS_PATH / f"{task_id}.yaml").exists():
                problems.append(f"task file still exists: {TASKS_PATH / f'{task_id}.yaml'}")

        for run_id in dict.fromkeys(self.run_ids):
            try:
                delete_run(run_id)
            except Exception as exc:  # noqa: BLE001
                problems.append(f"run {run_id}: {exc!r}")
            if (RUNS_PATH / run_id).exists():
                problems.append(f"run folder still exists: {RUNS_PATH / run_id}")
            with contextlib.suppress(Exception):
                if build_suggestion_store().list_attempts(run_id):
                    problems.append(f"suggestion rows still exist for run {run_id}")

        if problems:
            raise AssertionError("E2E cleanup left residue:\n" + "\n".join(problems))

    def _manifest_entity_ids(self) -> set[str]:
        """Every sharedId the workspace's manifests say a run touched."""
        ids: set[str] = set()
        for run_id in self.run_ids:
            try:
                manifest = build_backup_store().load_manifest(run_id)
            except Exception:  # noqa: BLE001 - a never-created step has no manifest
                continue
            for entry in [*manifest.created, *manifest.deleted]:
                if entry.shared_id:
                    ids.add(entry.shared_id)
                if entry.restored_shared_id:
                    ids.add(entry.restored_shared_id)
        return ids

    def _known_entity_ids(self) -> set[str]:
        return set(self.entity_ids) | self._manifest_entity_ids()

    def _delete_template_entities(self, problems: list[str]) -> None:
        assert self.client is not None
        assert self.template_id is not None
        known = self._known_entity_ids()
        try:
            known |= {row.shared_id for row in self._search_template(self.languages.default) if row.shared_id}
        except Exception as exc:  # noqa: BLE001 - template may already be gone
            logger.warning("cleanup could not search template {}: {}", self.namespace, exc)

        for shared_id in sorted(known):
            try:
                self.client.entities.delete(shared_id)
            except Exception as exc:  # noqa: BLE001 - a 404 is already gone
                message = str(exc).lower()
                if "404" not in message and "not found" not in message:
                    problems.append(f"entity {shared_id}: {exc!r}")

    def _delete_template(self, problems: list[str]) -> None:
        assert self.client is not None
        assert self.template_id is not None
        for _attempt in range(3):
            try:
                self.client.templates.delete_empty_template(self.template_id)
                break
            except Exception as exc:  # noqa: BLE001 - retry after entity deletion settles
                message = str(exc)
                if "not found" in message.lower() or "404" in message:
                    break
                time.sleep(0.5)
        else:
            problems.append(f"template {self.template_id} could not be deleted")
        try:
            if self.client.templates.get_by_name(self.namespace) is not None:
                problems.append(f"template {self.namespace!r} still exists")
        except Exception as exc:  # noqa: BLE001
            problems.append(f"template verification failed: {exc!r}")


__all__ = [
    "AdminE2EWorkspace",
    "DEFAULT_PROPERTY",
    "LanguageProfile",
    "SECOND_PROPERTY",
    "UWAZI_PASSWORD",
    "UWAZI_URL",
    "UWAZI_USER",
    "comparable",
    "create_entity_script",
    "delete_entities_script",
    "field_diff",
    "metadata_diff",
    "text_value",
    "update_languages_script",
    "update_script",
    "update_template_script",
]
