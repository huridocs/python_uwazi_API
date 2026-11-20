"""Offline tests for the in-memory credential vault lifecycle (pure helpers)."""

from uwazi_admin_agent.drivers.web.session import (
    SessionCredentials,
    load_entry,
    store_entry,
)

_CREDS = SessionCredentials(instance_url="http://localhost:3000", user="admin", password="secret")


def test_store_then_load_returns_the_credentials() -> None:
    vault: dict = {}
    store_entry(vault, "tok", _CREDS, now=100.0, ttl_seconds=10.0)
    entry = load_entry(vault, "tok", now=105.0)
    assert entry is not None
    assert entry.credentials == _CREDS


def test_expired_entry_is_dropped() -> None:
    vault: dict = {}
    store_entry(vault, "tok", _CREDS, now=100.0, ttl_seconds=10.0)
    assert load_entry(vault, "tok", now=110.0) is None
    assert "tok" not in vault


def test_store_prunes_expired_entries() -> None:
    vault: dict = {}
    store_entry(vault, "old", _CREDS, now=0.0, ttl_seconds=5.0, max_entries=10)
    store_entry(vault, "live", _CREDS, now=10.0, ttl_seconds=100.0, max_entries=10)
    assert "old" not in vault
    assert "live" in vault


def test_store_evicts_the_oldest_when_the_cap_is_hit() -> None:
    vault: dict = {}
    store_entry(vault, "first", _CREDS, now=20.0, ttl_seconds=100.0, max_entries=2)
    store_entry(vault, "second", _CREDS, now=21.0, ttl_seconds=100.0, max_entries=2)
    store_entry(vault, "third", _CREDS, now=22.0, ttl_seconds=100.0, max_entries=2)
    assert "first" not in vault
    assert "second" in vault
    assert "third" in vault
