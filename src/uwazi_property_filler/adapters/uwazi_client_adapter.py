"""Uwazi I/O through the existing ``uwazi_api`` client + ``uwazi_agent`` mapper.

One :class:`UwaziApiAdapter` is kept per instance; its ``entity_mapper`` performs
the label/UUID and relationship coercion on the write path (``update_metadata``)
and the inverse on read (``get_entity_metadata``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_api.domain.constants import LANGUAGE_TO_FILE_LANGUAGE
from uwazi_api.domain.search_filters import SearchFilters, SelectFilter
from uwazi_property_filler.configuration import FILTER_PROPERTY, SUBTITLE_PROPERTY
from uwazi_property_filler.domain.metadata_text import metadata_text, metadata_value
from uwazi_property_filler.domain.pdf_item import PdfItem
from uwazi_property_filler.ports.uwazi_port import UwaziPort

# Elasticsearch ``index.max_result_window`` default: the largest window a single
# Uwazi search request can return, so filtered fetches page by this size.
_SEARCH_WINDOW = 10_000


class UwaziClientAdapter(UwaziPort):
    def __init__(self, user: str, password: str, url: str):
        self._user = user
        self._password = password
        self._url = url
        self._adapter: UwaziApiAdapter | None = None

    def _ensure_adapter(self) -> UwaziApiAdapter:
        if self._adapter is None:
            # Constructing UwaziApiAdapter performs the real /api/login and
            # raises AuthenticationError on bad credentials.
            self._adapter = UwaziApiAdapter(user=self._user, password=self._password, url=self._url)
        return self._adapter

    @property
    def client(self):
        return self._ensure_adapter().client

    async def login(self, user: str, password: str) -> None:
        await asyncio.to_thread(self.login_sync, user, password)

    def login_sync(self, user: str, password: str) -> None:
        # Constructing UwaziApiAdapter performs the real /api/login and
        # raises AuthenticationError on bad credentials.
        adapter = UwaziApiAdapter(user=user, password=password, url=self._url)
        self._adapter = adapter
        self._user = user
        self._password = password

    async def list_documents(self, template_name: str, language: str, filter_value: str | None = None) -> list[PdfItem]:
        def _fetch() -> list[PdfItem]:
            adapter = self._ensure_adapter()
            if filter_value is None:
                entities = adapter.client.search.get(template_name=template_name, batch_size=10000, language=language)
            else:
                # One request per filter value, paged: Uwazi caps a single
                # search at Elasticsearch's 10 000-result window, so each value
                # is fetched in consecutive windows until exhausted.
                filters = SearchFilters()
                filters.add(FILTER_PROPERTY, SelectFilter(values=[filter_value]))
                entities: list[Any] = []
                start_from = 0
                while True:
                    page = adapter.client.search.search_by_filter(
                        filters,
                        template_name=template_name,
                        start_from=start_from,
                        batch_size=_SEARCH_WINDOW,
                        language=language,
                    )
                    entities.extend(page)
                    if len(page) < _SEARCH_WINDOW:
                        break  # exhausted the value's matching set
                    start_from += _SEARCH_WINDOW
            file_language = LANGUAGE_TO_FILE_LANGUAGE.get(language)
            items: list[PdfItem] = []
            for entity in entities:
                docs = [d for d in entity.documents if d.language == file_language]
                if not docs:
                    continue
                items.append(
                    PdfItem(
                        shared_id=entity.shared_id or "",
                        title=entity.title or "",
                        subtitle=metadata_text(metadata_value(entity.metadata or {}, SUBTITLE_PROPERTY)),
                        template_name=template_name,
                        filename=docs[0].filename,
                        language=language,
                    )
                )
            if SUBTITLE_PROPERTY:
                matched = sum(1 for item in items if item.subtitle)
                logger.info(
                    "list_documents: subtitle property '{}' matched {}/{} documents "
                    "(0 usually means the configured name does not exist on template {})",
                    SUBTITLE_PROPERTY,
                    matched,
                    len(items),
                    template_name,
                )
            return items

        return await asyncio.to_thread(_fetch)

    async def get_entity_metadata(self, shared_id: str, template_name: str, language: str) -> dict[str, Any]:
        def _fetch() -> dict[str, Any]:
            adapter = self._ensure_adapter()
            entity = adapter.client.entities.get_one(shared_id, language)
            agent = adapter.entity_mapper.to_agent(entity, template_name, language)
            return agent.metadata

        return await asyncio.to_thread(_fetch)

    async def update_metadata(self, shared_id: str, template_name: str, language: str, metadata: dict[str, Any]) -> None:
        def _update() -> None:
            adapter = self._ensure_adapter()

            async def _call() -> None:
                results = await adapter.update_entities(
                    [
                        AgentEntity(
                            shared_id=shared_id,
                            title=None,
                            template_name=template_name,
                            metadata=metadata,
                            language=language,
                        )
                    ],
                    language,
                )
                for result in results:
                    if not result.success:
                        raise RuntimeError(f"Uwazi update failed for {shared_id}: {result.error}")

            asyncio.run(_call())

        await asyncio.to_thread(_update)

    async def get_pdf_bytes(self, filename: str) -> bytes | None:
        def _fetch() -> bytes | None:
            adapter = self._ensure_adapter()
            return adapter.client.files.get_document_by_file_name(filename)

        return await asyncio.to_thread(_fetch)

    async def search_entities(self, template_name: str, language: str, query: str | None) -> list[dict[str, str]]:
        def _search() -> list[dict[str, str]]:
            adapter = self._ensure_adapter()
            entities = adapter.client.search.get(template_name=template_name, batch_size=10000, language=language)
            result: list[dict[str, str]] = []
            for entity in entities:
                title = entity.title or ""
                if query and query.lower() not in title.lower():
                    continue
                result.append({"shared_id": entity.shared_id or "", "title": title})
            return result

        return await asyncio.to_thread(_search)
