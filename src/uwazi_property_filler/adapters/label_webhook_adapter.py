"""Outbound HTTP webhook adapter for label changes.

POSTs a ``{event, label}`` JSON body to every configured URL. Failures are
logged and never raised: a webhook outage must not block saving the label.
"""

from __future__ import annotations

import httpx
from loguru import logger

from uwazi_property_filler.domain.label import Label
from uwazi_property_filler.ports.label_notify_port import LabelNotifyPort

_TIMEOUT = 10.0


class LabelWebhookAdapter(LabelNotifyPort):
    def __init__(self, urls: list[str]):
        self._urls = [u.rstrip("/") for u in urls if u.strip()]

    async def notify_created(self, label: Label) -> None:
        await self._post("label_created", label)

    async def notify_deleted(self, label: Label) -> None:
        await self._post("label_deleted", label)

    async def _post(self, event: str, label: Label) -> None:
        if not self._urls:
            return
        payload = {"event": event, "label": label.model_dump(mode="json")}
        for url in self._urls:
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    await client.post(url, json=payload)
            except Exception as exc:  # noqa: BLE001 — a webhook failure must not break a label save
                logger.warning("Label webhook POST to {} failed: {}", url, exc)
