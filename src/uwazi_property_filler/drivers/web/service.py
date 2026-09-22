"""Async service layer for the property-filler web UI (no NiceGUI imports).

Built once per process (module-level singleton keyed on the current login) and
rebuilt on login so credentials are always the validated session's.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from uwazi_property_filler.adapters.extension_stats import error_count
from uwazi_property_filler.adapters.pdf_cache_store import PdfCacheStore
from uwazi_property_filler.adapters.postgres_store import PostgresStore
from uwazi_property_filler.adapters.uwazi_client_adapter import UwaziClientAdapter
from uwazi_property_filler.configuration import DATABASE_URL, FILTER_PROPERTY, INSTANCE_KEY
from uwazi_api.domain.thesauri_label import qualify_label
from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.use_cases import extensions_use_case, list_pdfs_use_case, refresh_cache_use_case
from uwazi_property_filler.use_cases.get_suggestions_use_case import get_suggestions
from uwazi_property_filler.use_cases.validate_fill_use_case import validate_fill


class PropertyFillerService:
    def __init__(self, uwazi: UwaziClientAdapter, store: PostgresStore, cache: PdfCacheStore):
        self.uwazi = uwazi
        self.store = store
        self.cache = cache
        self.runners: list[ExtensionPort] = []
        self.highlighters: list[ExtensionPort] = []
        self.displayers: list[ExtensionPort] = []

    async def build_runners(self) -> None:
        suggestions = await extensions_use_case.get_enabled(self.store, ExtensionCategory.SUGGESTION)
        highlighters = await extensions_use_case.get_enabled(self.store, ExtensionCategory.HIGHLIGHTER)
        displayers = await extensions_use_case.get_enabled(self.store, ExtensionCategory.DISPLAYER)
        self.runners = extensions_use_case.build_runners(suggestions)
        self.highlighters = extensions_use_case.build_runners(highlighters)
        self.displayers = extensions_use_case.build_runners(displayers)

    def _ctx(
        self,
        shared_id: str,
        template: str,
        language: str,
        filename: str,
        metadata: dict,
        properties: list[str],
    ) -> ExtensionContext:
        return ExtensionContext(
            shared_id=shared_id,
            template_name=template,
            language=language,
            filename=filename,
            current_metadata=metadata,
            properties=properties,
        )

    async def login(self, user: str, password: str) -> bool:
        await self.uwazi.login(user, password)
        return True

    def login_sync(self, user: str, password: str) -> None:
        self.uwazi.login_sync(user, password)

    async def list_templates(self) -> list[str]:
        def _fetch() -> list[str]:
            return [t.name for t in self.uwazi.client.templates.get()]

        return await asyncio.to_thread(_fetch)

    async def list_pdfs(
        self, template: str, language: str, filter_value: str | None = None
    ) -> tuple[list[PdfItem], list[PdfItem]]:
        return await list_pdfs_use_case.list_pdfs(self.uwazi, self.store, INSTANCE_KEY, template, language, filter_value)

    async def refresh(self, template: str, language: str, filter_value: str | None = None) -> int:
        return await refresh_cache_use_case.refresh(self.uwazi, self.cache, template, language, filter_value)

    async def get_entity_metadata(self, shared_id: str, template: str, language: str) -> dict[str, Any]:
        return await self.uwazi.get_entity_metadata(shared_id, template, language)

    async def get_template_properties(self, template: str) -> list[Any]:
        def _fetch() -> list[Any]:
            tpl = self.uwazi.client.templates.get_by_name(template)
            if tpl is None:
                return []
            return list(tpl.properties)

        return await asyncio.to_thread(_fetch)

    async def get_filter_options(self, template: str, language: str) -> dict[str, str]:
        """Filter options as ``{value: display_label}``.

        Display labels are the first 20 characters of the option name without
        its group; the value is the (possibly qualified) label that resolves
        back to the thesaurus id in ``search_by_filter``.
        """

        def _fetch() -> dict[str, str]:
            prop = self.uwazi.client.templates.find_property(template, FILTER_PROPERTY)
            if prop is None or not prop.content:
                return {}
            for thesaurus in self.uwazi.client.thesauris.get(language):
                if thesaurus.id == prop.content:
                    options: dict[str, str] = {}
                    for v in thesaurus.values:
                        if v.values:
                            for child in v.values:
                                options[qualify_label(v.label, child.label)] = child.label[:20]
                        else:
                            options[v.label] = v.label[:20]
                    return options
            return {}

        return await asyncio.to_thread(_fetch)

    async def get_thesaurus_labels(self, thesaurus_id: str, language: str) -> list[str]:
        def _fetch() -> list[str]:
            labels: list[str] = []
            for thesaurus in self.uwazi.client.thesauris.get(language):
                if thesaurus.id == thesaurus_id:
                    for v in thesaurus.values:
                        labels.append(v.label)
                        labels.extend(_child_labels(v))
                    break
            return labels

        return await asyncio.to_thread(_fetch)

    async def search_entities(self, template: str, language: str, query: str | None) -> list[dict[str, str]]:
        return await self.uwazi.search_entities(template, language, query)

    async def get_pdf_bytes(self, filename: str) -> bytes | None:
        cached = await self.cache.get(filename)
        if cached is not None:
            return cached
        data = await self.uwazi.get_pdf_bytes(filename)
        if data is not None:
            await self.cache.put(filename, data)
        return data

    async def get_suggestions(
        self,
        shared_id: str,
        template: str,
        language: str,
        filename: str,
        current_metadata: dict[str, Any],
        properties: list[str],
    ) -> list[Any]:
        ctx = self._ctx(shared_id, template, language, filename, current_metadata, properties)
        return await get_suggestions(ctx, self.runners, self.store, INSTANCE_KEY)

    async def get_highlights(
        self,
        shared_id: str,
        template: str,
        language: str,
        filename: str,
        current_metadata: dict[str, Any],
        properties: list[str],
    ) -> list[Any]:
        ctx = self._ctx(shared_id, template, language, filename, current_metadata, properties)
        result = []
        for runner in self.highlighters:
            result.extend(await runner.highlight(ctx))
        return result

    async def get_display(
        self,
        shared_id: str,
        template: str,
        language: str,
        filename: str,
        current_metadata: dict[str, Any],
        properties: list[str],
    ) -> str | None:
        ctx = self._ctx(shared_id, template, language, filename, current_metadata, properties)
        for runner in self.displayers:
            result = await runner.display(ctx)
            if result is not None:
                return result
        return None

    async def template_name_for_id(self, template_id: str | None) -> str | None:
        if not template_id:
            return None

        def _fetch() -> str | None:
            tpl = self.uwazi.client.templates.get_by_id(template_id)
            return tpl.name if tpl else None

        return await asyncio.to_thread(_fetch)

    async def validate(
        self,
        shared_id: str,
        template: str,
        language: str,
        metadata: dict[str, Any],
        extension_ids: list[str],
        before: dict[str, Any],
    ) -> None:
        await validate_fill(
            self.uwazi,
            self.store,
            self.store,
            INSTANCE_KEY,
            shared_id,
            template,
            language,
            metadata,
            extension_ids,
            before,
        )

    async def list_extensions(self) -> list[ExtensionRecord]:
        return await extensions_use_case.list_extensions(self.store)

    async def set_extension_enabled(self, extension_id: str, enabled: bool) -> None:
        await extensions_use_case.set_enabled(self.store, extension_id, enabled)
        await self.build_runners()

    async def register_extension(self, record: ExtensionRecord) -> None:
        await extensions_use_case.register(self.store, record)
        await self.build_runners()

    async def extension_stats(self, extension_id: str) -> dict[str, int]:
        def _count() -> int:
            with self.store._pool.connection() as conn:  # noqa: SLF001
                row = conn.execute(
                    "SELECT count(*) AS n FROM suggestion WHERE extension_id = %s", (extension_id,)
                ).fetchone()
                return int(row["n"]) if row else 0

        processed = await asyncio.to_thread(_count)
        return {"processed": processed, "errors": error_count(extension_id)}


def _child_labels(value: Any) -> list[str]:
    labels: list[str] = []
    for child in value.values or []:
        labels.append(child.label)
        labels.extend(_child_labels(child))
    return labels


_service: PropertyFillerService | None = None


def build_service(user: str, password: str) -> PropertyFillerService:
    """Composition root: one adapter + store + cache per validated login."""
    url = os.environ["UWAZI_URL"]
    uwazi = UwaziClientAdapter(user=user, password=password, url=url)
    store = PostgresStore(DATABASE_URL)
    cache = PdfCacheStore()
    return PropertyFillerService(uwazi, store, cache)


def get_service() -> PropertyFillerService | None:
    return _service


def set_service(service: PropertyFillerService | None) -> None:
    global _service
    _service = service
