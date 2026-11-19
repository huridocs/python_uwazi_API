"""Isolated unit tests for the session credential vault (offline, no mocks).

The vault is real: a real ``FilePersistentDict`` in ``tmp_path``, a real
``Request`` scope, and the real ``session`` helpers. The point of the module is
that the PASSWORD never reaches the persisted document, and that a logged-out
session loses both its credentials and its claim on an instance.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from nicegui import core
from nicegui import storage as ng_storage
from nicegui.persistence import FilePersistentDict
from starlette.requests import Request

from uwazi_admin_agent.drivers.web import session as s

PASSWORD = "hunter2-never-on-disk"
INSTANCE = "http://instance-a.test"


def _request(session_id: str) -> Request:
    return Request(
        {
            "type": "http",
            "path": "/app",
            "root_path": "",
            "query_string": b"",
            "session": {"id": session_id},
            "headers": [],
            "server": ("testserver", 80),
            "scheme": "http",
            "client": ("127.0.0.1", 1),
        }
    )


@pytest.fixture()
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real per-session storage document, wired into NiceGUI's registry."""
    monkeypatch.setattr(ng_storage.Storage, "secret", "test-secret")
    monkeypatch.setattr(ng_storage.Storage, "path", tmp_path / ".nicegui")
    session_id = uuid.uuid4().hex
    document = FilePersistentDict(tmp_path / f"storage-user-{session_id}.json", encoding="utf-8")
    ng_storage.request_contextvar.set(_request(session_id))
    core.app.storage._users[session_id] = document
    try:
        yield document
    finally:
        s.forget_credentials()
        core.app.storage._users.pop(session_id, None)
        ng_storage.request_contextvar.set(None)


def test_login_persists_no_password(storage) -> None:
    s.remember_credentials(INSTANCE, "admin", PASSWORD)

    persisted = json.dumps(dict(storage))
    assert PASSWORD not in persisted
    assert "password" not in storage
    assert set(storage) == {"credential_token", "user", "instance_url"}


def test_the_session_resolves_its_credentials_from_memory(storage) -> None:
    s.remember_credentials(INSTANCE, "admin", PASSWORD)

    credentials = s.session_credentials()
    assert credentials is not None
    assert (credentials.instance_url, credentials.user, credentials.password) == (INSTANCE, "admin", PASSWORD)
    assert s.session_instance_url() == INSTANCE


def test_credentials_are_scoped_to_their_own_instance(storage) -> None:
    s.remember_credentials(INSTANCE, "admin", PASSWORD)

    assert s.credentials_for_instance(INSTANCE) is not None
    assert s.credentials_for_instance(INSTANCE + "/") is not None
    assert s.credentials_for_instance("http://instance-b.test") is None
    assert s.session_is_active("http://instance-b.test") is False


def test_logout_drops_the_credentials_and_the_persisted_fields(storage) -> None:
    s.remember_credentials(INSTANCE, "admin", PASSWORD)

    s.forget_credentials()

    assert s.session_credentials() is None
    assert s.session_instance_url() == ""
    assert dict(storage) == {}


def test_a_legacy_plaintext_password_is_scrubbed_on_login(storage) -> None:
    """Builds before the vault wrote ``password`` into the session document."""
    storage["password"] = "legacy-plaintext"

    s.remember_credentials(INSTANCE, "admin", PASSWORD)

    assert "password" not in storage


def test_an_unknown_or_missing_token_resolves_nothing(storage) -> None:
    assert s.session_credentials() is None
    storage["credential_token"] = "not-a-real-token"
    assert s.session_credentials() is None
