"""Isolated tests for the raw entity repository's revert-exact save (per AGENTS.md).

No network: an in-process HTTP double (a real object implementing the tiny
``get``/``post_json`` surface the adapter uses) records what the adapter would
send and serves a canned current row. This pins the merge-clearing rule that
makes revert exact — the bug it prevents (Uwazi's entity POST MERGES metadata,
so a snapshot save silently leaves a script-added key live) was found by a live
apply→revert cycle.
"""

import asyncio
import json
from typing import Any

from uwazi_admin_agent.adapters.entity_repository_adapter import UwaziEntityRepository


class _Response:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.content = json.dumps(payload).encode()


class _RequestAdapter:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def get(self, **kwargs: Any) -> _Response:
        return _Response(200, {"rows": self.rows})

    def delete(self, **kwargs: Any) -> _Response:
        return _Response(200, {})


class _Http:
    """The minimal slice of the client's http surface the adapter touches."""

    def __init__(self, rows: list[dict[str, Any]], status: int = 200) -> None:
        self.url = "http://instance"
        self.headers: dict[str, str] = {}
        self.request_adapter = _RequestAdapter(rows)
        self.sent: list[dict[str, Any]] = []
        self._status = status

    def post_json(self, url: str, json: dict[str, Any], cookies: dict[str, Any] | None = None) -> _Response:
        self.sent.append(json)
        return _Response(self._status, {})


class _Client:
    def __init__(self, rows: list[dict[str, Any]], status: int = 200) -> None:
        self.http = _Http(rows, status)


def _repo(rows: list[dict[str, Any]], status: int = 200) -> tuple[UwaziEntityRepository, _Client]:
    client = _Client(rows, status)
    return UwaziEntityRepository(client), client  # type: ignore[arg-type]


# --- clearing keys the snapshot does not have --------------------------------


def test_save_raw_clears_a_key_present_live_but_absent_from_the_snapshot() -> None:
    """Uwazi's POST MERGES metadata, so revert must clear keys the script added."""
    live = {"sharedId": "S1", "language": "en", "metadata": {"a": [{"value": "x"}], "added": [{"value": "y"}]}}
    repo, client = _repo([live])
    snapshot = {"sharedId": "S1", "language": "en", "title": "T", "metadata": {"a": [{"value": "x"}]}}

    asyncio.run(repo.save_raw(snapshot))

    assert len(client.http.sent) == 1
    sent_metadata = client.http.sent[0]["metadata"]
    assert sent_metadata["added"] == []  # explicitly cleared
    assert sent_metadata["a"] == [{"value": "x"}]  # restored value untouched


def test_save_raw_sends_the_raw_unchanged_when_nothing_is_extra() -> None:
    live = {"sharedId": "S1", "language": "en", "metadata": {"a": [{"value": "x"}]}}
    repo, client = _repo([live])
    snapshot = {"sharedId": "S1", "language": "en", "title": "T", "metadata": {"a": [{"value": "old"}]}}

    asyncio.run(repo.save_raw(snapshot))

    # Only the snapshot's own key is sent; no invented clears.
    assert client.http.sent[0]["metadata"] == {"a": [{"value": "old"}]}


def test_save_raw_clears_every_extra_key() -> None:
    live = {"sharedId": "S1", "language": "en", "metadata": {"a": [], "b": [], "c": []}}
    repo, client = _repo([live])
    snapshot = {"sharedId": "S1", "language": "en", "title": "T", "metadata": {"a": []}}

    asyncio.run(repo.save_raw(snapshot))

    assert client.http.sent[0]["metadata"] == {"a": [], "b": [], "c": []}


def test_save_raw_tolerates_a_row_that_cannot_be_read() -> None:
    """A momentary absence must not block the save: fall back to the plain raw."""
    repo, client = _repo([])  # the read returns no rows ⇒ get_raw raises
    snapshot = {"sharedId": "S1", "language": "en", "title": "T", "metadata": {"a": []}}

    asyncio.run(repo.save_raw(snapshot))

    assert client.http.sent[0]["metadata"] == {"a": []}


def test_save_raw_without_a_shared_id_sends_the_raw_unchanged() -> None:
    repo, client = _repo([])
    raw = {"language": "en", "title": "T", "metadata": {"a": []}}
    asyncio.run(repo.save_raw(raw))
    assert client.http.sent[0] == raw


def test_save_raw_handles_metadata_that_is_not_a_dict() -> None:
    live = {"sharedId": "S1", "language": "en", "metadata": {"a": []}}
    repo, client = _repo([live])
    snapshot = {"sharedId": "S1", "language": "en", "title": "T", "metadata": "broken"}

    asyncio.run(repo.save_raw(snapshot))

    # The snapshot's metadata is replaced by a dict carrying the live extra key.
    assert client.http.sent[0]["metadata"] == {"a": []}


def test_save_raw_surfaces_a_non_200() -> None:
    live = {"sharedId": "S1", "language": "en", "metadata": {}}
    repo, client = _repo([live], status=422)
    try:
        asyncio.run(repo.save_raw({"sharedId": "S1", "language": "en", "metadata": {}}))
    except RuntimeError as exc:
        assert "save_raw failed (422)" in str(exc)
    else:  # pragma: no cover - the raise above is the expected path
        raise AssertionError("expected RuntimeError")


def test_get_raw_by_shared_id_returns_the_first_row() -> None:
    row = {"sharedId": "S1", "language": "en", "metadata": {"a": []}}
    repo, _client = _repo([row])
    assert asyncio.run(repo.get_raw_by_shared_id("S1", "en")) == row
