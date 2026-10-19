"""``UwaziClient``-backed drift probe over the ``/api/v2/search`` route.

Reuses the request conventions :class:`UwaziSearchProbe` established for the
same route: the route always applies a ``term: { language }`` filter from the
request locale, so the adapter sets ``Accept-Language`` + the ``locale``
cookie per call; ``fields[]=sharedId&fields[]=editDate`` collapses each hit
to the two fields the comparison needs (``buildQuery`` maps ``query.fields``
onto Elastic ``_source.includes``); ``sort=-editDate`` rides the indexed
``editDate.sort`` sub-field; ``page.limit``/``page.offset`` drive pagination.

``count_by_template`` goes to ``/api/v2/entities/count_by_template``
(the Mongo distinct-sharedId count) — the deletion signal ``editDate`` can
never show.

Errors raise (the port documents it); :mod:`uwazi_admin_agent.drivers.web.run_service`
decides the verdict's fall-back behavior. Sync ``requests`` calls inside async
signatures, matching the other adapters.
"""

from __future__ import annotations

import json
from typing import Any, override

from loguru import logger

from uwazi_admin_agent.ports.drift_probe_port import DriftProbePort
from uwazi_api.client import UwaziClient

_MAX_RESULT_WINDOW: int = 10000  # Elastic's index.max_result_window


def _parse_dates(rows: list[dict[str, Any]]) -> dict[str, int | None]:
    """``sharedId -> editDate`` from search hits; non-numeric editDates map to None."""
    out: dict[str, int | None] = {}
    for row in rows:
        shared_id = row.get("sharedId")
        if not isinstance(shared_id, str) or not shared_id:
            continue
        raw = row.get("editDate")
        edit_date: int | None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            try:
                edit_date = int(str(raw))
            except (TypeError, ValueError):
                edit_date = None
        else:
            edit_date = int(raw)
        out[shared_id] = edit_date
    return out


class UwaziDriftProbe(DriftProbePort):
    """EditDate/count probes over a :class:`UwaziClient`."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    def _search(self, template_id: str, language: str, limit: int, sort: bool) -> dict[str, int | None]:
        """One paged ``/api/v2/search`` call reduced to sharedId/editDate."""
        params: list[tuple[str, Any]] = [
            ("filter[template]", template_id),
            ("fields[]", "sharedId"),
            ("fields[]", "editDate"),
            ("page[limit]", limit),
        ]
        if sort:
            params.append(("sort", "-editDate"))
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/v2/search",
            headers={**self._client.http.headers, "Accept-Language": language},
            params=params,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"drift search failed: status={response.status_code}, body={body[:300]}")
        data = json.loads(response.content).get("data", [])
        return _parse_dates(data if isinstance(data, list) else [])

    @override
    def probe_recent(self, template_id: str, language: str, limit: int = 10) -> dict[str, int | None]:
        return self._search(template_id, language, limit, sort=True)

    @override
    def scan_template(self, template_id: str, language: str, page_size: int = 10000) -> dict[str, int | None]:
        """Paged full scan (unsorted; the window cap forces one page per 10k)."""
        collected: dict[str, int | None] = {}
        offset = 0
        size = min(page_size, _MAX_RESULT_WINDOW)
        while True:
            page = self._search_with_offset(template_id, language, size, offset)
            if not page:
                break
            collected.update(page)
            if len(page) < size:
                break
            offset += size
            if offset >= _MAX_RESULT_WINDOW * 10:  # defensive runaway cap
                logger.warning("drift scan hit 100k cap template={}", template_id)
                break
        return collected

    def _search_with_offset(self, template_id: str, language: str, limit: int, offset: int) -> dict[str, int | None]:
        params: list[tuple[str, Any]] = [
            ("filter[template]", template_id),
            ("fields[]", "sharedId"),
            ("fields[]", "editDate"),
            ("page[limit]", limit),
            ("page[offset]", offset),
        ]
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/v2/search",
            headers={**self._client.http.headers, "Accept-Language": language},
            params=params,
            cookies={"locale": language},
        )
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            raise RuntimeError(f"drift scan failed: status={response.status_code}, body={body[:300]}")
        data = json.loads(response.content).get("data", [])
        return _parse_dates(data if isinstance(data, list) else [])

    @override
    def count_by_template(self, template_id: str) -> int:
        response = self._client.http.request_adapter.get(
            url=f"{self._client.http.url}/api/v2/entities/count_by_template",
            headers=self._client.http.headers,
            params={"templateId": template_id},
        )
        if response.status_code != 200:
            raise RuntimeError(f"count_by_template failed: status={response.status_code}")
        value = json.loads(response.content)
        if isinstance(value, dict):
            value = value.get("count", 0)
        return int(value)
