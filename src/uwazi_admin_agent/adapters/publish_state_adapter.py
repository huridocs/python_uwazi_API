"""``UwaziClient``-backed publish/permission state (see :mod:`ports.publish_state_port`).

Reads ``GET /api/entities?include[]=permissions`` (Uwazi only returns the
``permissions`` array when the caller is authenticated and asks for it), and
writes ``POST /api/entities/permissions`` — the endpoint Uwazi's own publish
flow uses. ``published`` is derived from the presence of the ``public`` grant,
exactly as the server does.

Async by signature (the port is async); the underlying ``requests`` calls are
synchronous, matching :class:`UwaziEntityRepository`.
"""

from __future__ import annotations

import json
from typing import Any, override

from loguru import logger

from uwazi_admin_agent.domain.publish_state import PublishState, public_entry_present
from uwazi_admin_agent.ports.publish_state_port import PublishStatePort
from uwazi_api.client import UwaziClient


def read_params(shared_id: str) -> list[tuple[str, Any]]:
    """The ``/api/entities`` query for one entity's publish state (pure).

    ``include`` is an ARRAY in the endpoint's query schema: sending it as a
    scalar string is rejected with ``/query/include must be array`` (400), which
    used to leave every backup snapshot without a publish state. The repeated
    ``include[]`` key is the encoding Express's ``qs`` parses into
    ``["permissions"]``.
    """
    return [
        ("sharedId", shared_id),
        ("include[]", "permissions"),
        ("omitRelationships", "true"),
    ]


class UwaziPublishStateRepository(PublishStatePort):
    """Publish/permission grants over a :class:`UwaziClient`."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def read(self, shared_id: str, language: str | None = None) -> PublishState:
        params = read_params(shared_id)
        cookies = {"locale": language} if language else {}
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/entities",
            headers=self._client.http.headers,
            params=params,
            cookies=cookies,
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"publish state read failed ({response.status_code}) for {shared_id}: {body}")
        rows = json.loads(response.content).get("rows", [])
        if not rows:
            raise RuntimeError(f"Entity not found for sharedId={shared_id} (publish state)")
        row = rows[0]
        permissions = row.get("permissions") or []
        return PublishState(
            published=bool(row.get("published", public_entry_present(permissions))),
            permissions=[p for p in permissions if isinstance(p, dict)],
        )

    @override
    async def restore(self, shared_id: str, state: PublishState) -> None:
        """Post the captured grants verbatim.

        The endpoint replaces the entity's grants with the posted list (its
        handler derives ``isPublic`` from it), which is what makes this an
        exact restore rather than a publish/unpublish toggle: a permission
        that was REMOVED by the run is re-added, and one it ADDED is dropped.
        """
        payload = {"ids": [shared_id], "permissions": list(state.permissions)}
        response = self._client.http.request_adapter.post(
            url=f"{self._client.http.url}/api/entities/permissions",
            headers=self._client.http.headers,
            cookies={},
            data=json.dumps(payload),
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"publish state restore failed ({response.status_code}) for {shared_id}: {body}")
        logger.info("publish state restored sharedId={} grants={}", shared_id, len(state.permissions))
