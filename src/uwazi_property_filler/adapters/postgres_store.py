"""PostgreSQL persistence for the registry, per-PDF status, suggestions, and audit.

A sync ``psycopg_pool.ConnectionPool`` wrapped in ``asyncio.to_thread`` at each
async boundary, matching the repo's sync-under-async adapter style. The schema
(``schema.sql``) is applied once on first construction.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_kind import ExtensionKind
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.fill_audit import FillAuditRecord
from uwazi_property_filler.domain.fill_status import FillStatus
from uwazi_property_filler.domain.highlight import Highlight
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.ports.audit_store_port import AuditStorePort
from uwazi_property_filler.ports.document_store_port import DocumentStorePort
from uwazi_property_filler.ports.extension_registry_port import ExtensionRegistryPort
from uwazi_property_filler.ports.suggestion_store_port import SuggestionStorePort

_SCHEMA_PATH = Path(__file__).parent.parent / "schema.sql"


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


class PostgresStore(DocumentStorePort, SuggestionStorePort, AuditStorePort, ExtensionRegistryPort):
    def __init__(self, database_url: str):
        self._pool = ConnectionPool(database_url, min_size=1, max_size=4, kwargs={"row_factory": dict_row})
        self._apply_schema()

    def _apply_schema(self) -> None:
        ddl = _SCHEMA_PATH.read_text()
        with self._pool.connection() as conn:
            conn.execute(ddl)

    # --- ExtensionRegistryPort -------------------------------------------

    async def list_extensions(self) -> list[ExtensionRecord]:
        return await asyncio.to_thread(self._list_extensions_sync)

    def _list_extensions_sync(self) -> list[ExtensionRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, name, kind, category, enabled, endpoint, entrypoint, config FROM extension ORDER BY name"
            ).fetchall()
        return [
            ExtensionRecord(
                id=r["id"],
                name=r["name"],
                kind=ExtensionKind(r["kind"]),
                category=ExtensionCategory(r["category"]),
                enabled=r["enabled"],
                endpoint=r["endpoint"],
                entrypoint=r["entrypoint"],
                config=r["config"] if isinstance(r["config"], dict) else json.loads(r["config"] or "{}"),
            )
            for r in rows
        ]

    async def get_enabled(self, category: ExtensionCategory) -> list[ExtensionRecord]:
        records = await self.list_extensions()
        return [r for r in records if r.enabled and r.category == category]

    async def save_extension(self, record: ExtensionRecord) -> None:
        await asyncio.to_thread(self._save_extension_sync, record)

    def _save_extension_sync(self, record: ExtensionRecord) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO extension (id, name, kind, category, enabled, endpoint, entrypoint, config)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    kind = EXCLUDED.kind,
                    category = EXCLUDED.category,
                    enabled = EXCLUDED.enabled,
                    endpoint = EXCLUDED.endpoint,
                    entrypoint = EXCLUDED.entrypoint,
                    config = EXCLUDED.config
                """,
                (
                    record.id,
                    record.name,
                    record.kind.value,
                    record.category.value,
                    record.enabled,
                    record.endpoint,
                    record.entrypoint,
                    _dumps(record.config),
                ),
            )

    async def set_enabled(self, extension_id: str, enabled: bool) -> None:
        await asyncio.to_thread(self._set_enabled_sync, extension_id, enabled)

    def _set_enabled_sync(self, extension_id: str, enabled: bool) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE extension SET enabled = %s WHERE id = %s", (enabled, extension_id))

    # --- DocumentStorePort ------------------------------------------------

    async def upsert_status(self, instance_key: str, item: PdfItem) -> None:
        await asyncio.to_thread(self._upsert_status_sync, instance_key, item)

    def _upsert_status_sync(self, instance_key: str, item: PdfItem) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO document_status (instance_key, shared_id, language, status, template_name, title, filename)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (instance_key, shared_id, language) DO UPDATE SET
                    template_name = EXCLUDED.template_name,
                    title = EXCLUDED.title,
                    filename = EXCLUDED.filename
                """,
                (
                    instance_key,
                    item.shared_id,
                    item.language,
                    item.status.value,
                    item.template_name,
                    item.title,
                    item.filename,
                ),
            )

    async def list_by_status(self, instance_key: str, status: FillStatus, language: str) -> list[PdfItem]:
        return await asyncio.to_thread(self._list_by_status_sync, instance_key, status, language)

    def _list_by_status_sync(self, instance_key: str, status: FillStatus, language: str) -> list[PdfItem]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT shared_id, template_name, title, filename, language, status
                FROM document_status
                WHERE instance_key = %s AND status = %s AND language = %s
                ORDER BY title
                """,
                (instance_key, status.value, language),
            ).fetchall()
        return [
            PdfItem(
                shared_id=r["shared_id"],
                title=r["title"] or "",
                template_name=r["template_name"] or "",
                filename=r["filename"] or "",
                language=r["language"],
                status=FillStatus(r["status"]),
            )
            for r in rows
        ]

    async def get_status(self, instance_key: str, shared_id: str, language: str) -> PdfItem | None:
        return await asyncio.to_thread(self._get_status_sync, instance_key, shared_id, language)

    def _get_status_sync(self, instance_key: str, shared_id: str, language: str) -> PdfItem | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                SELECT shared_id, template_name, title, filename, language, status
                FROM document_status
                WHERE instance_key = %s AND shared_id = %s AND language = %s
                """,
                (instance_key, shared_id, language),
            ).fetchone()
        if row is None:
            return None
        return PdfItem(
            shared_id=row["shared_id"],
            title=row["title"] or "",
            template_name=row["template_name"] or "",
            filename=row["filename"] or "",
            language=row["language"],
            status=FillStatus(row["status"]),
        )

    async def mark_validated(self, instance_key: str, shared_id: str, language: str, filled_metadata: dict) -> None:
        await asyncio.to_thread(self._mark_validated_sync, instance_key, shared_id, language, filled_metadata)

    def _mark_validated_sync(self, instance_key: str, shared_id: str, language: str, filled_metadata: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                UPDATE document_status
                SET status = %s, filled_metadata = %s, validated_at = now()
                WHERE instance_key = %s AND shared_id = %s AND language = %s
                """,
                (FillStatus.VALIDATED.value, _dumps(filled_metadata), instance_key, shared_id, language),
            )

    # --- SuggestionStorePort ----------------------------------------------

    async def save_suggestions(self, instance_key: str, shared_id: str, suggestions: list[Suggestion]) -> None:
        await asyncio.to_thread(self._save_suggestions_sync, instance_key, shared_id, suggestions)

    def _save_suggestions_sync(self, instance_key: str, shared_id: str, suggestions: list[Suggestion]) -> None:
        rows = [
            (
                instance_key,
                shared_id,
                s.source_extension_id,
                s.property_name,
                _dumps(s.value),
                s.confidence,
                _dumps([h.model_dump() for h in s.highlights]) if s.highlights else None,
            )
            for s in suggestions
        ]
        if not rows:
            return
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.executemany(
                    """
                    INSERT INTO suggestion (
                        instance_key, shared_id, extension_id, property_name,
                        value, confidence, highlights
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    rows,
                )

    async def list_for(self, instance_key: str, shared_id: str) -> list[Suggestion]:
        return await asyncio.to_thread(self._list_for_sync, instance_key, shared_id)

    def _list_for_sync(self, instance_key: str, shared_id: str) -> list[Suggestion]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT extension_id, property_name, value, confidence, highlights
                FROM suggestion
                WHERE instance_key = %s AND shared_id = %s
                ORDER BY id
                """,
                (instance_key, shared_id),
            ).fetchall()
        result: list[Suggestion] = []
        for r in rows:
            hl = r["highlights"]
            highlights = [Highlight(**h) for h in (hl if isinstance(hl, list) else json.loads(hl or "[]"))]
            result.append(
                Suggestion(
                    property_name=r["property_name"],
                    value=r["value"],
                    confidence=r["confidence"] or 1.0,
                    source_extension_id=r["extension_id"],
                    highlights=highlights,
                )
            )
        return result

    # --- AuditStorePort ---------------------------------------------------

    async def save_audit(self, record: FillAuditRecord) -> None:
        await asyncio.to_thread(self._save_audit_sync, record)

    def _save_audit_sync(self, record: FillAuditRecord) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO fill_audit (
                    instance_key, shared_id, property_name, before_value,
                    after_value, extension_ids, validated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    record.instance_key,
                    record.shared_id,
                    record.property_name,
                    _dumps(record.before_value) if record.before_value is not None else None,
                    _dumps(record.after_value),
                    _dumps(record.extension_ids),
                    record.validated_at,
                ),
            )

    async def list_audit(self, instance_key: str, shared_id: str) -> list[FillAuditRecord]:
        return await asyncio.to_thread(self._list_audit_sync, instance_key, shared_id)

    def _list_audit_sync(self, instance_key: str, shared_id: str) -> list[FillAuditRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT property_name, before_value, after_value, extension_ids, validated_at
                FROM fill_audit
                WHERE instance_key = %s AND shared_id = %s
                ORDER BY id
                """,
                (instance_key, shared_id),
            ).fetchall()
        result: list[FillAuditRecord] = []
        for r in rows:
            result.append(
                FillAuditRecord(
                    instance_key=instance_key,
                    shared_id=shared_id,
                    property_name=r["property_name"],
                    before_value=r["before_value"],
                    after_value=r["after_value"],
                    extension_ids=(
                        r["extension_ids"]
                        if isinstance(r["extension_ids"], list)
                        else json.loads(r["extension_ids"] or "[]")
                    ),
                    validated_at=r["validated_at"],
                )
            )
        return result
