"""Isolated unit tests: post-revert verification on MULTILINGUAL entities (per AGENTS.md).

A Uwazi sharedId can carry one entity ROW per configured language. Each row has
its own ``_id``, its own ``language`` and its own i18n-TRANSLATED metadata
``label`` values, so a raw captured in ``es`` is only ever comparable against
the ``es`` row. These tests pin the regression that had verification fetching
language-agnostically: the server answered with the ``en`` row, and every
multilingual entity in the run was reported as a mismatch on ``_id``,
``language`` and every translated label.

Real in-memory ports (not mocks), literal snapshots, plain assertions. No
network, no filesystem, no environment.
"""

from datetime import datetime, timezone
from typing import Any, override

import pytest

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RunStatus
from uwazi_admin_agent.domain.relationship_restore import extract_inbound_refs_from_existing
from uwazi_admin_agent.domain.snapshot import EntitySnapshot
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.verify_revert_use_case import VerifyRevertUseCase

pytestmark = pytest.mark.anyio


# --- in-memory ports (real classes, not mocks) --------------------------------


class MultilingualEntityRepository(EntityRepositoryPort):
    """Stores one raw per (sharedId, language), mirroring Uwazi's language rows.

    A language-agnostic read returns the FIRST row stored — the same "whichever
    row the server picks" behaviour that made the verification compare the es
    snapshot against the en row.
    """

    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self._rows: dict[str, list[dict[str, Any]]] = {sid: list(raws) for sid, raws in (rows or {}).items()}
        self.fetches: list[tuple[str, str | None]] = []

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        self.fetches.append((shared_id, language))
        rows = self._rows.get(shared_id)
        if not rows:
            raise RuntimeError(f"Entity not found: {shared_id}")
        if language is None:
            return dict(rows[0])
        for raw in rows:
            if raw.get("language") == language:
                return dict(raw)
        # Uwazi falls back to its default locale when the requested row is absent.
        return dict(rows[0])

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        for rows in self._rows.values():
            for raw in rows:
                if raw.get("_id") == internal_id:
                    return dict(raw)
        raise RuntimeError(f"Entity not found by _id: {internal_id}")

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        raise NotImplementedError("VerifyRevertUseCase never writes")

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        raise NotImplementedError("VerifyRevertUseCase never creates")

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self._rows.pop(shared_id, None)


class InMemoryBackupStore(BackupStorePort):
    def __init__(self) -> None:
        self._manifests: dict[str, MigrationManifest] = {}
        self._snapshots: dict[str, dict[str, EntitySnapshot]] = {}

    @override
    def save_snapshot(self, run_id: str, snapshot: EntitySnapshot) -> None:
        self._snapshots.setdefault(run_id, {})[snapshot.shared_id] = snapshot

    @override
    def load_snapshot(self, run_id: str, shared_id: str) -> EntitySnapshot:
        if run_id not in self._snapshots or shared_id not in self._snapshots[run_id]:
            raise FileNotFoundError(f"No snapshot for run={run_id} sharedId={shared_id}")
        return self._snapshots[run_id][shared_id]

    @override
    def save_file_bytes(self, run_id: str, shared_id: str, file_id: str, data: bytes) -> None: ...

    @override
    def load_file_bytes(self, run_id: str, shared_id: str, file_id: str) -> bytes:
        raise FileNotFoundError("not used by these tests")

    @override
    def save_manifest(self, run_id: str, manifest: MigrationManifest) -> None:
        self._manifests[run_id] = manifest

    @override
    def load_manifest(self, run_id: str) -> MigrationManifest:
        if run_id not in self._manifests:
            raise FileNotFoundError(f"No manifest for run={run_id}")
        return self._manifests[run_id]

    @override
    def update_status(self, run_id: str, status: RunStatus) -> None:
        self._manifests[run_id].status = status

    @override
    def clear_run(self, run_id: str) -> None:
        self._snapshots.pop(run_id, None)

    @override
    def list_runs(self) -> list[str]:
        return sorted(self._manifests.keys())

    @override
    def delete_run(self, run_id: str) -> None:
        self._manifests.pop(run_id, None)
        self._snapshots.pop(run_id, None)

    @override
    def rename_run(self, old_id: str, new_id: str) -> None:
        raise NotImplementedError("not used by these tests")


# --- helpers ------------------------------------------------------------------


def _now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=timezone.utc)


def _row(language: str, internal_id: str, title: str, **overrides: Any) -> dict[str, Any]:
    """One language row of an entity, with the translated labels Uwazi derives per locale."""
    labels = {"en": ("Statement", "United Nations"), "es": ("Comunicado", "Naciones Unidas")}
    statement, united = labels[language]
    raw: dict[str, Any] = {
        "sharedId": "hq6qzufxwud",
        "_id": internal_id,
        "language": language,
        "title": title,
        "editDate": 1789129181561,
        "metadata": {
            "document_type": [{"value": "272469a4", "label": statement, "parent": {"label": united}}],
            "document_ref": [{"value": "A/HRC/37/NGO/9"}],
        },
    }
    raw.update(overrides)
    return raw


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=_now(),
    )


def _manifest(
    modified: list[EntityIdentity] | None = None,
    deleted: list[EntityIdentity] | None = None,
    created: list[EntityIdentity] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="update 3",
        created_at=_now(),
        prompt="p",
        script="x = 1",
        modified=modified or [],
        deleted=deleted or [],
        created=created or [],
        status=RunStatus.REVERTED,
    )


def _rel(entity: str, hub: str, template: str | None, language: str | None = None) -> dict[str, Any]:
    """One hub relation row, carrying the endpoint's embedded ``entityData``."""
    row: dict[str, Any] = {"entity": entity, "hub": hub, "template": template}
    if language is not None:
        row["entityData"] = {"sharedId": entity, "language": language}
    return row


# --- the reported failure: es snapshot vs en row ------------------------------


async def test_multilingual_modified_entity_verifies_against_its_own_language_row() -> None:
    """A successfully reverted es row must verify ok, not be flagged against the en row.

    The raw shape mirrors the reported failure: identical data, but the two rows
    differ in ``_id``, ``language`` and every i18n-TRANSLATED label.
    """
    es = _row("es", "6aa3f1dd9a2fa07cbf84c60a", "Titulo")
    en = _row("en", "6aa3f1dd9a2fa07cbf84c609", "Title")
    repo = MultilingualEntityRepository({"hq6qzufxwud": [en, es]})  # en first: the server's default
    store = InMemoryBackupStore()
    store.save_snapshot("update 3", _snapshot("hq6qzufxwud", es))
    store.save_manifest("update 3", _manifest(modified=[EntityIdentity(shared_id="hq6qzufxwud", language="es")]))

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert result.ok is True
    assert result.mismatches == []


async def test_verification_fetches_the_recorded_language_row() -> None:
    """The locale is threaded into the read, not left to the server's default."""
    es = _row("es", "es1", "Titulo")
    repo = MultilingualEntityRepository({"hq6qzufxwud": [_row("en", "en1", "Title"), es]})
    store = InMemoryBackupStore()
    store.save_snapshot("update 3", _snapshot("hq6qzufxwud", es))
    store.save_manifest("update 3", _manifest(modified=[EntityIdentity(shared_id="hq6qzufxwud", language="es")]))

    await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert repo.fetches == [("hq6qzufxwud", "es")]


async def test_verification_falls_back_to_the_snapshot_language_for_an_older_manifest() -> None:
    """A manifest entry recorded before ``language`` existed still targets the right row."""
    es = _row("es", "es1", "Titulo")
    repo = MultilingualEntityRepository({"hq6qzufxwud": [_row("en", "en1", "Title"), es]})
    store = InMemoryBackupStore()
    store.save_snapshot("update 3", _snapshot("hq6qzufxwud", es))
    store.save_manifest("update 3", _manifest(modified=[EntityIdentity(shared_id="hq6qzufxwud")]))

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert result.ok is True
    assert repo.fetches == [("hq6qzufxwud", "es")]


async def test_a_real_mismatch_inside_the_recorded_row_is_still_caught() -> None:
    """Language awareness must not blind the check: a wrong es title still fails."""
    repo = MultilingualEntityRepository({"hq6qzufxwud": [_row("en", "en1", "Title"), _row("es", "es1", "MAL")]})
    store = InMemoryBackupStore()
    store.save_snapshot("update 3", _snapshot("hq6qzufxwud", _row("es", "es1", "Titulo")))
    store.save_manifest("update 3", _manifest(modified=[EntityIdentity(shared_id="hq6qzufxwud", language="es")]))

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert result.ok is False
    assert [m.shared_id for m in result.mismatches] == ["hq6qzufxwud"]


async def test_deleted_entity_recreated_is_fetched_in_its_recreate_language() -> None:
    """The re-created row is addressed by its NEW sharedId, in the recreate's locale."""
    recreated = _row("es", "new1", "Titulo")
    recreated["sharedId"] = "newD"
    repo = MultilingualEntityRepository({"newD": [recreated]})
    store = InMemoryBackupStore()
    snapshot_raw = _row("es", "old1", "Titulo")
    snapshot_raw["sharedId"] = "D"
    store.save_snapshot("update 3", _snapshot("D", snapshot_raw))
    store.save_manifest(
        "update 3",
        _manifest(deleted=[EntityIdentity(shared_id="D", language="es", restored_shared_id="newD")]),
    )

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert result.ok is True
    assert repo.fetches == [("newD", "es")]


async def test_created_entity_is_fetched_in_its_recorded_language() -> None:
    """A created entry carries no snapshot, so its own recorded language is used."""
    repo = MultilingualEntityRepository({"C": [_row("en", "c1", "Created")]})
    store = InMemoryBackupStore()
    store.save_manifest("update 3", _manifest(created=[EntityIdentity(shared_id="C", language="en")]))

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert result.ok is False  # the created entity survived the revert
    assert repo.fetches == [("C", "en")]


# --- inbound refs on a still-existing multilingual entity ---------------------


async def test_inbound_ref_endpoint_is_fetched_in_the_languages_own_row() -> None:
    """The still-existing endpoint's hub carries its language, so the gap check reads that row."""
    snapshot_relations = [
        _rel("B", "h1", None, language="es"),
        _rel("A", "h1", "rtype1", language="es"),
    ]
    recreated = {
        "sharedId": "newA",
        "_id": "na1",
        "language": "es",
        "title": "A",
        "template": "tmplA",
        "metadata": {},
        "relations": [_rel("newA", "h1", None), _rel("B", "h1", "rtype1")],
    }
    restored_b = {
        "sharedId": "B",
        "_id": "b1",
        "language": "es",
        "title": "B",
        "template": "tmplB",
        "metadata": {},
        "relations": [_rel("B", "h1", None), _rel("newA", "h1", "rtype1")],
    }
    other_b = dict(restored_b, _id="b2", language="en", relations=[])
    repo = MultilingualEntityRepository({"newA": [recreated], "B": [other_b, restored_b]})
    store = InMemoryBackupStore()
    store.save_snapshot(
        "update 3",
        _snapshot("A", {"sharedId": "A", "_id": "a1", "language": "es", "title": "A", "relations": snapshot_relations}),
    )
    store.save_manifest(
        "update 3",
        _manifest(deleted=[EntityIdentity(shared_id="A", language="es", restored_shared_id="newA")]),
    )

    result = await VerifyRevertUseCase(entity_repository=repo, backup_store=store).verify("update 3")

    assert ("B", "es") in repo.fetches
    assert result.relationship_gaps == []


# --- the pure extraction -----------------------------------------------------


def test_inbound_ref_records_the_existing_entitys_language() -> None:
    """``entityData.language`` on the hub's FROM row becomes the ref's language."""
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "sharedId": "A",
                "_id": "a1",
                "language": "es",
                "template": "tmplA",
                "relations": [
                    _rel("B", "h1", None, language="es"),
                    _rel("A", "h1", "rtype1", language="es"),
                ],
            },
        )
    }

    refs = extract_inbound_refs_from_existing(snapshots, {"A"})

    assert [(r.existing_shared_id, r.deleted_shared_id, r.existing_language) for r in refs] == [("B", "A", "es")]


def test_inbound_ref_language_is_none_when_the_row_carries_no_entity_data() -> None:
    """Older/leaner relation rows simply fall back to a language-agnostic fetch."""
    snapshots = {"A": _snapshot("A", {"sharedId": "A", "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")]})}

    refs = extract_inbound_refs_from_existing(snapshots, {"A"})

    assert [(r.existing_shared_id, r.existing_language) for r in refs] == [("B", None)]
