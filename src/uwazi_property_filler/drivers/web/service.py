"""Async service layer for the property-filler web UI (no NiceGUI imports).

Built once per process (module-level singleton keyed on the current login) and
rebuilt on login so credentials are always the validated session's.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

from loguru import logger

from uwazi_api.domain.selection_rectangle import SelectionRectangle
from uwazi_api.domain.thesauri_label import qualify_label
from uwazi_property_filler.adapters import document_cache_file
from uwazi_property_filler.adapters.extension_stats import error_count
from uwazi_property_filler.adapters.label_webhook_adapter import LabelWebhookAdapter
from uwazi_property_filler.adapters.pdf_cache_store import PdfCacheStore
from uwazi_property_filler.adapters.postgres_store import PostgresStore
from uwazi_property_filler.adapters.uwazi_client_adapter import UwaziClientAdapter
from uwazi_property_filler.configuration import (
    DATABASE_URL,
    FILTER_PROPERTY,
    INSTANCE_KEY,
    LABEL_RELATIONSHIP_TYPE,
    LABEL_WEBHOOK_URLS,
    PROPERTY_TEMPLATE,
)
from uwazi_property_filler.domain.document_cache import DocumentCache
from uwazi_property_filler.domain.extension_category import ExtensionCategory
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.label import Label, LabelPrediction
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.ports.label_notify_port import LabelNotifyPort
from uwazi_property_filler.use_cases import extensions_use_case, label_use_case, list_pdfs_use_case, refresh_cache_use_case
from uwazi_property_filler.use_cases.get_predictions_use_case import get_predictions
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
        self.labelers: list[ExtensionPort] = []
        self.notifiers: list[ExtensionPort] = []
        self.label_webhooks: list[LabelNotifyPort] = [LabelWebhookAdapter(LABEL_WEBHOOK_URLS)]
        # Document lists are pulled from Uwazi only by ``refresh`` (Connect &
        # Refresh); every later read is served from this cache. A snapshot is
        # written to disk on refresh/validate and reloaded here after a
        # restart, so lists and filter counts come back without re-connecting.
        self._documents: DocumentCache | None = None
        self._documents_lock = asyncio.Lock()

    async def _ensure_documents(self, template: str, language: str) -> None:
        """Hydrate the in-memory cache from the persisted snapshot, once."""
        if self._documents is not None:
            return
        async with self._documents_lock:
            if self._documents is not None:
                return
            documents = await asyncio.to_thread(document_cache_file.load, template, language)
            if documents is None:
                return
            self._documents = documents
            logger.info(
                "Restored {} cached documents for template {} ({}) from snapshot",
                documents.count(None),
                template,
                language,
            )

    async def _persist_documents(self) -> None:
        """Write the current cache snapshot to disk (best effort)."""
        documents = self._documents
        if documents is None:
            return
        try:
            await asyncio.to_thread(document_cache_file.save, documents)
        except Exception as exc:  # noqa: BLE001 — a snapshot failure must not fail the refresh
            logger.warning("Could not persist the document cache snapshot: {}", exc)

    async def build_runners(self) -> None:
        suggestions = await extensions_use_case.get_enabled(self.store, ExtensionCategory.SUGGESTION)
        highlighters = await extensions_use_case.get_enabled(self.store, ExtensionCategory.HIGHLIGHTER)
        displayers = await extensions_use_case.get_enabled(self.store, ExtensionCategory.DISPLAYER)
        labelers = await extensions_use_case.get_enabled(self.store, ExtensionCategory.LABELER)
        notifiers = await extensions_use_case.get_enabled(self.store, ExtensionCategory.NOTIFIER)
        self.runners = extensions_use_case.build_runners(suggestions)
        self.highlighters = extensions_use_case.build_runners(highlighters)
        self.displayers = extensions_use_case.build_runners(displayers)
        self.labelers = extensions_use_case.build_runners(labelers)
        self.notifiers = extensions_use_case.build_runners(notifiers)

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
        """(pending, validated) for one filter bucket, served from the cache.

        The cache is filled by :meth:`refresh` (``Connect & Refresh``) and,
        after a restart, reloaded from its on-disk snapshot — so the lists are
        populated immediately on the first read once a snapshot exists. Only a
        very first run (no snapshot yet) returns empty lists.
        """
        await self._ensure_documents(template, language)
        if self._documents is None:
            return [], []
        return self._documents.split(filter_value)

    async def refresh(self, template: str, language: str) -> int:
        """Fetch every filter bucket from Uwazi once and replace the cache.

        Returns the total number of cached documents (the ALL bucket). This is
        the fast half of Connect & Refresh (document metadata only); the slow
        half is :meth:`warm_pdf_cache`, which the UI runs afterwards so the
        new lists can render without waiting for every PDF download.
        """
        documents = await self._fetch_document_cache(template, language)
        async with self._documents_lock:
            self._documents = documents
        await self._persist_documents()
        return documents.count(None)

    async def warm_pdf_cache(self, template: str, language: str) -> int:
        """Download and disk-cache every document's PDF bytes.

        Returns the number of PDFs cached. Separated from :meth:`refresh`
        because it is by far the slowest step of Connect & Refresh.
        """
        return await refresh_cache_use_case.refresh(self.uwazi, self.cache, template, language, None)

    async def _fetch_document_cache(self, template: str, language: str) -> DocumentCache:
        """One Uwazi request per filter value (plus the ALL bucket), paged
        under the 10 000-result search window, upserting statuses as before."""
        values = list(self._filter_values_sync(template, language).keys())
        documents = DocumentCache(template=template, language=language)
        for value in [None, *values]:
            pending, validated = await list_pdfs_use_case.list_pdfs(
                self.uwazi, self.store, INSTANCE_KEY, template, language, value
            )
            documents.pending[value] = pending
            documents.validated[value] = validated
        return documents

    async def get_entity_metadata(self, shared_id: str, template: str, language: str) -> dict[str, Any]:
        return await self.uwazi.get_entity_metadata(shared_id, template, language)

    async def save_entity_metadata(self, shared_id: str, template: str, language: str, metadata: dict[str, Any]) -> None:
        """Partial metadata update straight to Uwazi, without validation side effects.

        Used by the live checkbox toggles: every mark/unmark is persisted
        immediately, while "Mark as validated" (:meth:`validate`) additionally
        marks the document validated and audits the change.
        """
        await self.uwazi.update_metadata(shared_id, template, language, metadata)

    async def get_template_properties(self, template: str) -> list[Any]:
        def _fetch() -> list[Any]:
            tpl = self.uwazi.client.templates.get_by_name(template)
            if tpl is None:
                return []
            return list(tpl.properties)

        return await asyncio.to_thread(_fetch)

    async def get_filter_options(self, template: str, language: str) -> dict[str, str]:
        """Filter options as ``{value: display_label}`` including document counts.

        The display label is the full (possibly group-qualified) option name
        followed by its document count, e.g. ``"HRC: Resolution (12)"``; the
        value is the (possibly qualified) label that resolves back to the
        thesaurus id in ``search_by_filter``. Counts come from the document
        cache — the same population the pending/validated lists show — so the
        numbers match the tabs. The cache is reloaded from its on-disk snapshot
        after a restart; only before the very first ``Connect & Refresh`` (and
        with no snapshot) does every count show 0.
        """
        await self._ensure_documents(template, language)
        options = await asyncio.to_thread(self._filter_values_sync, template, language)
        documents = self._documents
        counts = documents.count if documents is not None else (lambda _value: 0)
        return {value: f"{label} ({counts(value)})" for value, label in options.items()}

    def _filter_values_sync(self, template: str, language: str) -> dict[str, str]:
        """Option values → short labels from the filter property's thesaurus."""
        prop = self.uwazi.client.templates.find_property(template, FILTER_PROPERTY)
        if prop is None or not prop.content:
            return {}
        for thesaurus in self.uwazi.client.thesauris.get(language):
            if thesaurus.id == prop.content:
                options: dict[str, str] = {}
                for v in thesaurus.values:
                    if v.values:
                        for child in v.values:
                            options[qualify_label(v.label, child.label)] = child.label
                    else:
                        options[v.label] = v.label
                return options
        return {}

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
        if self._documents is not None:
            self._documents.mark_validated(shared_id)
            await self._persist_documents()

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

    # --- labels ------------------------------------------------------------

    async def get_label_values(self, language: str) -> list[dict[str, str]]:
        """Assignable label values (``[{"shared_id","title"}]``) from the value template."""
        if not PROPERTY_TEMPLATE:
            return []
        return await self.uwazi.search_entities(PROPERTY_TEMPLATE, language, None)

    async def get_labels(self, shared_id: str, template: str, language: str) -> list[Label]:
        """Manual labels (Uwazi) + cached predictions, for the viewer overlay."""
        relationship_type_id = await self._label_relationship_type_id()
        file_id = await self.uwazi.get_file_id(shared_id, language)

        labels: list[Label] = []
        if relationship_type_id and file_id:
            relations = await self.uwazi.list_relationships(shared_id, language)
            manual = label_use_case.labels_from_relations(
                relations,
                shared_id=shared_id,
                file_id=file_id,
                relationship_type_id=relationship_type_id,
            )
            titles = await self._label_titles([lbl.label_shared_id for lbl in manual], language)
            for lbl in manual:
                lbl.label_title = titles.get(lbl.label_shared_id, lbl.label_shared_id)
            labels.extend(manual)

        for prediction in await self.store.list_predictions(INSTANCE_KEY, shared_id):
            labels.append(self._label_from_prediction(prediction, shared_id, file_id or ""))
        return labels

    def _label_from_prediction(self, prediction: LabelPrediction, shared_id: str, file_id: str) -> Label:
        return Label(
            id=f"{prediction.source}:{prediction.label_shared_id}:{prediction.text}",
            shared_id=shared_id,
            file_id=file_id,
            label_shared_id=prediction.label_shared_id,
            label_title=prediction.label_title,
            text=prediction.text,
            selection_rectangles=prediction.selection_rectangles,
            page=prediction.page,
            source=prediction.source,
            confidence=prediction.confidence,
        )

    async def create_label(
        self,
        shared_id: str,
        template: str,
        language: str,
        text: str,
        rectangles: list[SelectionRectangle],
        label_shared_id: str,
        label_title: str,
    ) -> Label:
        """Persist a manual label as a Uwazi relationship, then notify."""
        relationship_type_id = await self._label_relationship_type_id()
        if not relationship_type_id:
            raise RuntimeError(f"Label relationship type '{LABEL_RELATIONSHIP_TYPE}' not found in Uwazi")
        file_id = await self.uwazi.get_file_id(shared_id, language)
        if not file_id:
            raise RuntimeError(f"Document {shared_id} has no file to label")

        label = Label(
            id=uuid4().hex,
            shared_id=shared_id,
            file_id=file_id,
            label_shared_id=label_shared_id,
            label_title=label_title,
            relationship_type_id=relationship_type_id,
            text=text,
            selection_rectangles=rectangles,
            page=_page_of(rectangles),
        )
        created = await label_use_case.create_label(
            self.uwazi, label, file_id=file_id, relationship_type_id=relationship_type_id, language=language
        )
        await label_use_case.notify_label(self.label_webhooks, self.notifiers, created, deleted=False)
        return created

    async def delete_label(self, shared_id: str, language: str, label: Label) -> None:
        """Delete a manual label's relationship, then notify."""
        await label_use_case.delete_label(self.uwazi, label, language=language)
        await label_use_case.notify_label(self.label_webhooks, self.notifiers, label, deleted=True)

    async def refresh_predictions(
        self,
        shared_id: str,
        template: str,
        language: str,
        filename: str,
        current_metadata: dict[str, Any],
        properties: list[str],
    ) -> list[LabelPrediction]:
        """Recompute predictions from enabled labelers and replace the cache."""
        ctx = self._ctx(shared_id, template, language, filename, current_metadata, properties)
        return await get_predictions(ctx, self.labelers, self.store, INSTANCE_KEY)

    async def _label_relationship_type_id(self) -> str | None:
        if not LABEL_RELATIONSHIP_TYPE:
            return None
        return await self.uwazi.relationship_type_id(LABEL_RELATIONSHIP_TYPE)

    async def _label_titles(self, shared_ids: list[str], language: str) -> dict[str, str]:
        if not shared_ids:
            return {}
        titles: dict[str, str] = {}
        if PROPERTY_TEMPLATE:
            try:
                for entry in await self.uwazi.search_entities(PROPERTY_TEMPLATE, language, None):
                    titles[entry["shared_id"]] = entry["title"]
            except Exception as exc:  # noqa: BLE001 — titles are best-effort
                logger.warning("Could not resolve label titles from template '{}': {}", PROPERTY_TEMPLATE, exc)
        missing = [sid for sid in shared_ids if sid not in titles]
        for sid in missing:
            try:
                title = await self._entity_title(sid, language)
                if title:
                    titles[sid] = title
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not resolve label title for {}: {}", sid, exc)
        return titles

    async def _entity_title(self, shared_id: str, language: str) -> str | None:
        def _fetch() -> str | None:
            return self.uwazi.client.entities.get_one(shared_id, language).title

        return await asyncio.to_thread(_fetch)


def _page_of(rectangles: list[SelectionRectangle]) -> int:
    if not rectangles:
        return 1
    try:
        return int(rectangles[0].page)
    except (TypeError, ValueError):
        return 1


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
