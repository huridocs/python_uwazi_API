"""UwaziClient-backed raw entity access (§2.5, Phase 4).

Reaches into ``UwaziClient.http`` for raw get/save/delete so the raw dict
round-trips without field loss, and into ``UwaziClient.templates`` only to
resolve template names for the search ``types`` param. Never builds a
validated ``Entity``. Async by signature (the port is async); the underlying
``requests`` calls are synchronous, matching ``uwazi_api``.
"""

import json
from typing import Any, cast, override

from loguru import logger

from uwazi_admin_agent.domain.filter import EntityFilter
from uwazi_admin_agent.domain.snapshot import EntityIdentity
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_api.client import UwaziClient

_SEARCH_LIMIT = 50


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
            headers=cast(dict[str, str], self._client.http.headers),
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
            headers=cast(dict[str, str], self._client.http.headers),
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
            headers=cast(dict[str, str], self._client.http.headers),
            params={"sharedId": shared_id},
            cookies={},
        )
        if response.status_code != 200:
            raise RuntimeError(f"delete_by_shared_id failed ({response.status_code}) for sharedId={shared_id}")
        logger.debug("raw deleted sharedId={}", shared_id)

    @override
    async def find_touch_set(self, entity_filter: EntityFilter) -> list[EntityIdentity]:
        # /api/search has no sharedId filter, so explicit shared_ids are resolved
        # by direct raw fetch (other criteria advisory in that branch, per the
        # Phase 4 decision). Otherwise /api/search AND-combines the criteria.
        if entity_filter.shared_ids:
            return await self._touch_set_from_shared_ids(entity_filter)
        return await self._touch_set_from_search(entity_filter)

    async def _touch_set_from_shared_ids(self, entity_filter: EntityFilter) -> list[EntityIdentity]:
        identities: list[EntityIdentity] = []
        assert entity_filter.shared_ids is not None
        for shared_id in entity_filter.shared_ids:
            raw = await self.get_raw_by_shared_id(shared_id, language=entity_filter.language)
            identities.append(
                EntityIdentity(
                    shared_id=raw["sharedId"],
                    internal_id=raw.get("_id"),
                    language=raw.get("language"),
                )
            )
        return identities

    async def _touch_set_from_search(self, entity_filter: EntityFilter) -> list[EntityIdentity]:
        template_id = self._resolve_template_id(entity_filter.template) if entity_filter.template else None
        language = entity_filter.language or "en"
        identities: list[EntityIdentity] = []
        offset = 0
        while True:
            params = self._search_params(entity_filter, template_id, offset)
            response = self._client.http.request_adapter.get(
                url=f"{self._client.http.url}/api/search",
                headers=cast(dict[str, str], self._client.http.headers),
                params=params,
                cookies={"locale": language},
            )
            rows = _rows(response, "find_touch_set search")
            for row in rows:
                shared_id = row.get("sharedId")
                if not shared_id:
                    continue
                identities.append(
                    EntityIdentity(
                        shared_id=shared_id,
                        internal_id=row.get("_id"),
                        language=row.get("language"),
                    )
                )
            if len(rows) < _SEARCH_LIMIT:
                break
            offset += _SEARCH_LIMIT
        return identities

    def _search_params(self, entity_filter: EntityFilter, template_id: str | None, offset: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "from": offset,
            "limit": _SEARCH_LIMIT,
            "includeUnpublished": "true",
            "allAggregations": "false",
            "sort": "creationDate",
            "order": "desc",
        }
        if template_id:
            params["types"] = f'["{template_id}"]'
        if entity_filter.search_text:
            params["searchTerm"] = entity_filter.search_text
            params["sort"] = "_score"
        return params

    def _resolve_template_id(self, template_name: str) -> str:
        template_id = self._client.templates.resolve_template_id(template_name)
        if not template_id:
            raise RuntimeError(f"Template not found: {template_name!r}")
        return template_id


def _rows(response: Any, what: str) -> list[dict[str, Any]]:
    if response.status_code != 200:
        body = response.content.decode("utf-8", errors="replace")
        raise RuntimeError(f"{what} failed ({response.status_code}): {body}")
    data = json.loads(response.content)
    return data.get("rows", [])
