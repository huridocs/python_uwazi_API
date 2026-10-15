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

from uwazi_admin_agent.domain.create_payload import to_create_payload
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
        """Upsert a raw entity dict via POST /api/entities (the locale cookie picks the row).

        ``POST /api/entities`` is a **merge** over the stored metadata: a key the
        payload omits keeps its stored value. That matters for revert — restoring
        a snapshot must also UNDO a property the script ADDED, and a snapshot
        predates that key, so omitting it would leave the added value live (and
        post-revert verification would correctly flag the mismatch).

        So the payload explicitly clears every key that exists on the entity now
        but is absent from the snapshot raw. An empty list is the shape Uwazi
        accepts for "this property holds nothing" across property types.
        """
        language = raw.get("language") or "en"
        payload = await self._with_explicit_clears(raw, language)
        response = self._client.http.post_json(
            url=f"{self._client.http.url}/api/entities",
            json=payload,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"save_raw failed ({response.status_code}): {body}")
        logger.debug("raw saved sharedId={}", raw.get("sharedId"))

    async def _with_explicit_clears(self, raw: dict[str, Any], language: str) -> dict[str, Any]:
        """Add empty-list entries for keys live now but not in the snapshot raw.

        Best-effort: if the current row cannot be read (e.g. this is a
        relationship-ref re-save on a row that is momentarily absent), the raw is
        sent unchanged — the merge cannot have added anything in that case either.
        """
        snapshot_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        shared_id = raw.get("sharedId")
        if not shared_id:
            return raw
        try:
            current = await self.get_raw_by_shared_id(shared_id, language)
        except Exception as exc:  # noqa: BLE001 — fall back to the plain save
            logger.debug("save_raw: could not read current row for {} ({})", shared_id, exc)
            return raw
        current_metadata = current.get("metadata") if isinstance(current.get("metadata"), dict) else {}
        extra_keys = [key for key in current_metadata if key not in snapshot_metadata]
        if not extra_keys:
            return raw
        logger.info("save_raw: clearing {} key(s) absent from the snapshot for {}", len(extra_keys), shared_id)
        merged = {**raw, "metadata": {**snapshot_metadata, **{key: [] for key in extra_keys}}}
        return merged

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        # Re-create a deleted entity via the create branch: POST a payload with no
        # sharedId so Uwazi mints a fresh sharedId/_id and restores the data fields
        # (title, template, icon, user, metadata, url-attachments). Returns the new
        # sharedId from the response body (the non-multipart create returns the
        # created entity dict). The locale cookie selects the row language.
        payload = to_create_payload(raw)
        language = raw.get("language") or "en"
        response = self._client.http.post_json(
            url=f"{self._client.http.url}/api/entities",
            json=payload,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"create_raw failed ({response.status_code}): {body}")
        created = json.loads(response.content)
        new_shared_id = created.get("sharedId")
        if not new_shared_id:
            raise RuntimeError(f"create_raw response missing sharedId: {created!r}")
        logger.info("raw re-created (new sharedId={}, from snapshot sharedId={})", new_shared_id, raw.get("sharedId"))
        return new_shared_id

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
