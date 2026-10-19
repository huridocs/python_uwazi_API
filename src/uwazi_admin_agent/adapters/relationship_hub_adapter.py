"""``UwaziClient``-backed connection-row access (see :mod:`ports.relationship_hub_port`).

Reads the entity's ``relations`` view (``GET /api/entities`` without
``omitRelationships`` — the same call the entity repository already makes) and
replays rows through ``POST /api/relationships/bulk``, the endpoint Uwazi's own
reference save uses. Does **not** modify ``uwazi_api``.

Async by signature (the port is async); the underlying ``requests`` calls are
synchronous, matching :class:`UwaziEntityRepository`.
"""

from __future__ import annotations

import json
from typing import Any, override

from loguru import logger

from uwazi_admin_agent.domain.connection_hubs import hub_groups
from uwazi_admin_agent.ports.relationship_hub_port import RelationshipHubPort
from uwazi_api.client import UwaziClient


class UwaziRelationshipHubs(RelationshipHubPort):
    """Raw connection-row access over a :class:`UwaziClient`."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def list_for_entity(self, shared_id: str, language: str | None = None) -> list[dict[str, Any]]:
        cookies = {"locale": language} if language else {}
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/entities",
            headers=self._client.http.headers,
            params={"sharedId": shared_id},
            cookies=cookies,
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"hub read failed ({response.status_code}) for {shared_id}: {body}")
        rows = json.loads(response.content).get("rows", [])
        if not rows:
            return []
        relations = rows[0].get("relations")
        return [r for r in relations if isinstance(r, dict)] if isinstance(relations, list) else []

    @override
    async def delete_rows(self, row_ids: list[str], language: str | None = None) -> None:
        if not row_ids:
            return
        payload = {"delete": [{"_id": row_id} for row_id in row_ids], "save": []}
        self._post_bulk(payload, language, what=f"delete {len(row_ids)} row(s)")

    @override
    async def restore_rows(self, rows: list[dict[str, Any]], language: str | None = None) -> None:
        if not rows:
            return
        payload = {"delete": [], "save": hub_groups(rows)}
        self._post_bulk(payload, language, what=f"restore {len(rows)} row(s)")

    def _post_bulk(self, payload: dict[str, Any], language: str | None, *, what: str) -> None:
        cookies = {"locale": language} if language else {}
        response = self._client.http.request_adapter.post(
            url=f"{self._client.http.url}/api/relationships/bulk",
            headers=self._client.http.headers,
            cookies=cookies,
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"connection bulk failed ({response.status_code}) to {what}: {body}")
        logger.info("connection rows {} via bulk", what)
