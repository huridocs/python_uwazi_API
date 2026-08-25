"""``UwaziClient``-backed ES search probe for the dummy-gate freshness settle (Option A, rev 2).

Delegates to ``GET /api/v2/search?filter[sharedId]=<id>&fields[]=sharedId
&fields[]=editDate`` over the same :class:`UwaziClient.http` the entity/file
repositories use, and parses the response with the pure
:func:`extract_edit_date` helper. Does **not** modify ``uwazi_api``; imports and
delegates, mirroring :class:`UwaziEntityRepository`.

The route filters ES by ``terms: { sharedId: [<id>] }`` and applies a
``term: { language }`` filter from the request locale, so the adapter sets both
the ``Accept-Language`` header and the ``locale`` cookie to the dummy language.
Requesting ``fields=[sharedId, editDate]`` makes the route return those fields
(``buildQuery`` uses ``query.fields`` for ``_source.includes``); ``editDate`` is
the monotonic "which version is refreshed" signal the settle compares to the
latest Mongo ``editDate``.

Any probe failure (non-200, network, parse) returns ``None`` so the settle keeps
polling to the deadline and ultimately surfaces an ``es_settle_warning`` rather
than crashing the gate (gate correctness is Mongo-based, not ES-based).

Async by signature (the port is async); the underlying ``requests`` call is
synchronous, matching the other repositories. Not unit-tested (I/O) - validated
live alongside the rest of the harness.
"""

from __future__ import annotations

import json
from typing import Any, override

from loguru import logger

from uwazi_admin_agent.domain.search_probe import extract_edit_date
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_api.client import UwaziClient

_DEFAULT_LANGUAGE: str = "en"


class UwaziSearchProbe(SearchProbePort):
    """ES search probe over a :class:`UwaziClient` (Option A, rev 2)."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def shared_id_edit_date(self, shared_id: str, language: str | None = None) -> int | None:
        locale = language or _DEFAULT_LANGUAGE
        headers = {**self._client.http.headers, "Accept-Language": locale}
        # ``fields`` is a repeated-key array (``fields[]=sharedId&fields[]=editDate``);
        # passing a list of tuples makes ``requests`` emit the repeated ``fields[]``
        # keys that Express's ``qs`` parses into the array the schema expects.
        params = [
            ("filter[sharedId]", shared_id),
            ("fields[]", "sharedId"),
            ("fields[]", "editDate"),
        ]
        try:
            response = self._client.http.request_adapter.get(
                url=f"{self._client.http.url}/api/v2/search",
                headers=headers,
                params=params,
                cookies={"locale": locale},
            )
        except Exception as exc:  # noqa: BLE001 — probe failure is non-fatal
            logger.warning("search probe GET failed sharedId={}: {}", shared_id, exc)
            return None
        if response.status_code != 200:
            body = response.content.decode("utf-8", errors="replace")
            logger.warning("search probe non-200 sharedId={} ({}): {}", shared_id, response.status_code, body[:200])
            return None
        try:
            search_json: dict[str, Any] = json.loads(response.content)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("search probe parse failed sharedId={}: {}", shared_id, exc)
            return None
        return extract_edit_date(search_json)
