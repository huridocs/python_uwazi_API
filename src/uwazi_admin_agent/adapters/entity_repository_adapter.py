"""UwaziClient-backed raw entity access for backup/restore (§2.5, v2).

Reaches into ``UwaziClient.http`` for raw get/save/delete so the raw dict
round-trips without field loss. Never builds a validated ``Entity``. Async by
signature (the port is async); the underlying ``requests`` calls are
synchronous, matching ``uwazi_api``.

In v2 entity discovery for script generation uses ``uwazi_agent``'s
``query_entities``; this adapter only backs up and restores raw entities
(``find_touch_set`` is gone — the touch set is CRUD-intercepted, §2.4).
"""

import json
from typing import Any, override

from loguru import logger

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_api.client import UwaziClient


class UwaziEntityRepository(EntityRepositoryPort):
    """Raw, high-fidelity entity access over a ``UwaziClient`` (§2.5)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        # No omitRelationships: the raw entity carries its `relations` (§8).
        params = {"sharedId": shared_id}
        cookies = {"locale": language} if language else {}
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/entities",
            headers=self._client.http.headers,
            params=params,
            cookies=cookies,
        )
        rows = _rows(response, f"get_raw_by_shared_id({shared_id})")
        if not rows:
            raise RuntimeError(f"Entity not found for sharedId={shared_id} language={language!r}")
        logger.debug("raw get sharedId={} rows={}", shared_id, len(rows))
        return rows[0]

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/entities",
            headers=self._client.http.headers,
            params={"_id": internal_id},  # no omitRelationships
            cookies={},
        )
        rows = _rows(response, f"get_raw_by_internal_id({internal_id})")
        if not rows:
            raise RuntimeError(f"Entity not found for _id={internal_id}")
        return rows[0]

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        # POST /api/entities upserts by (sharedId, locale); the locale cookie
        # selects the row language (§8). Posting the raw dict preserves fields.
        language = raw.get("language") or "en"
        response = self._client.http.post_json(
            url=f"{self._client.http.url}/api/entities",
            json=raw,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"save_raw failed ({response.status_code}): {body}")
        logger.debug("raw saved sharedId={}", raw.get("sharedId"))

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        response = self._client.http.request_adapter.delete(
            url=f"{self._client.http.url}/api/entities",
            headers=self._client.http.headers,
            params={"sharedId": shared_id},
            cookies={},
        )
        if response.status_code != 200:
            raise RuntimeError(f"delete_by_shared_id failed ({response.status_code}) for sharedId={shared_id}")
        logger.debug("raw deleted sharedId={}", shared_id)


def _rows(response: Any, what: str) -> list[dict[str, Any]]:
    if response.status_code != 200:
        body = response.content.decode("utf-8", errors="replace")
        raise RuntimeError(f"{what} failed ({response.status_code}): {body}")
    data = json.loads(response.content)
    return data.get("rows", [])
