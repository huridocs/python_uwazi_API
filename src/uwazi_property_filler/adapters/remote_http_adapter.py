"""Remote HTTP extension adapter: POSTs the context to ``{endpoint}/<method>``.

Failures (HTTP, timeout, parse) return the empty defaults and are logged — an
extension failure must never break a fill.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger

from uwazi_property_filler.adapters.extension_stats import record_error
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.highlight import Highlight
from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.ports.extension_port import ExtensionPort

_TIMEOUT = 10.0


class RemoteHttpAdapter(ExtensionPort):
    def __init__(self, record: ExtensionRecord):
        self.record = record
        self._endpoint = (record.endpoint or "").rstrip("/")

    async def _call(self, method: str, ctx: ExtensionContext, empty: Any, model: Any) -> Any:
        url = f"{self._endpoint}/{method}"
        payload = ctx.model_dump(mode="json")
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()
            if model is None:
                return data
            if isinstance(data, list):
                return [model.model_validate(item) for item in data]
            return model.model_validate(data)
        except Exception as exc:  # noqa: BLE001 — extension failure must never break a fill
            logger.error("Remote extension '{}' {} failed: {}", self.record.id, method, exc)
            record_error(self.record.id)
            return empty

    async def suggest(self, ctx: ExtensionContext) -> list[Suggestion]:
        return await self._call("suggest", ctx, [], Suggestion)

    async def highlight(self, ctx: ExtensionContext) -> list[Highlight]:
        return await self._call("highlight", ctx, [], Highlight)

    async def display(self, ctx: ExtensionContext) -> str | None:
        return await self._call("display", ctx, None, None)

    async def search(self, ctx: ExtensionContext) -> list[dict[str, str]]:
        return await self._call("search", ctx, [], None)

    async def fill(self, ctx: ExtensionContext) -> dict[str, Any] | None:
        return await self._call("fill", ctx, None, None)
