"""Isolated unit tests for the persistent cross-task file/entity cache.

Per ``AGENTS.md``: no mocks/stubs, no network, no env creds. The on-disk store
(:class:`FileCacheStore`) is exercised against a tmp dir created inside each
test; the read-through decorators (:class:`CachedFileRepository` /
:class:`CachedEntityRepository`) are driven against tiny REAL in-memory port
classes — the AGENTS.md-sanctioned pattern from ``test_move_files_helper.py``
— so the hit/miss/``None``/invalidation decisions are verified offline. The
clock is a plain callable and mtimes are driven with ``os.utime`` (real
inputs, no stand-ins). Everything here is deterministic and offline.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, override

import pytest

from uwazi_admin_agent.adapters.cached_entity_repository import CachedEntityRepository
from uwazi_admin_agent.adapters.cached_file_repository import CachedFileRepository
from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.domain.file_cache import (
    FileCacheStats,
    format_cache_stats,
    instance_dir_name,
    is_fresh,
    safe_cache_name,
)
from uwazi_admin_agent.ports.cache_invalidation_port import CacheInvalidationPort
from uwazi_admin_agent.ports.cache_stats_port import CacheStatsPort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort

# --- in-memory ports (real classes, not mocks) ------------------------------


class _InMemoryFileRepo(FileRepositoryPort):
    """A dict-backed FileRepositoryPort that counts its calls."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files
        self.get_calls: list[str] = []
        self.uploads: list[tuple[str, bytes, str, str | None, str, str]] = []

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        self.get_calls.append(filename)
        return self._files.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("document", data, shared_id, language, title, content_type))
        return True

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("attachment", data, shared_id, language, title, content_type))
        return True

    @override
    async def delete_file(self, file_id: str) -> bool:
        # The cache test suite never deletes files; the port grew the method for
        # the duplicate-file cleanup, so the in-memory repo records and accepts.
        self.uploads.append(("delete", b"", file_id, None, "", ""))
        return True


class _InMemoryEntityRepo(EntityRepositoryPort):
    """A dict-backed EntityRepositoryPort that counts its calls."""

    def __init__(self, raws: dict[str, dict[str, Any]]) -> None:
        self._raws = raws
        self.get_calls: list[str] = []
        self.internal_get_calls: list[str] = []
        self.deleted: list[str] = []

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        self.get_calls.append(shared_id)
        if shared_id not in self._raws:
            raise RuntimeError(f"Entity not found for sharedId={shared_id}")
        return dict(self._raws[shared_id])  # a fresh dict per call, like the API

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        self.internal_get_calls.append(internal_id)
        return {"_id": internal_id}

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        self._raws[raw["sharedId"]] = raw

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        self._raws["new-sid"] = raw
        return "new-sid"

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        self.deleted.append(shared_id)
        self._raws.pop(shared_id, None)


class _FakeClock:
    """A controllable ``time.time`` replacement — a plain callable, not a mock."""

    def __init__(self) -> None:
        self.now: float = 1000.0

    def __call__(self) -> float:
        return self.now


# --- helpers -----------------------------------------------------------------


def _store(base: Path, **overrides: Any) -> FileCacheStore:
    """A store over a tmp dir; eviction effectively off unless overridden."""
    kwargs: dict[str, Any] = {
        "root": base / "cache",
        "max_bytes": 10**9,
        "ttl_seconds": 600.0,
        "evict_scan_interval": 10**9,
    }
    kwargs.update(overrides)
    return FileCacheStore(**kwargs)


def _file_entry_path(base: Path, filename: str) -> Path:
    """The documented on-disk layout path for one cached file-bytes entry."""
    return base / "cache" / "files" / safe_cache_name(filename)


def _raw_entry_path(base: Path, shared_id: str, language: str | None) -> Path:
    """The documented on-disk layout path for one cached raw entry."""
    lang = language if language else "default"
    return base / "cache" / "entities" / safe_cache_name(shared_id) / f"{safe_cache_name(lang)}.json"


# --- pure helpers (domain) ----------------------------------------------------


def test_safe_cache_name_is_deterministic_and_collision_free() -> None:
    assert safe_cache_name("doc.html") == safe_cache_name("doc.html")
    assert "/" not in safe_cache_name("../evil")
    assert "\\" not in safe_cache_name("a\\b")
    # Keys differing only in unsafe chars must not collide after sanitizing.
    assert safe_cache_name("a/b") != safe_cache_name("a_b")
    assert safe_cache_name("")  # even an empty key maps to a usable name


def test_instance_dir_name_is_stable_and_per_instance() -> None:
    assert instance_dir_name("http://localhost:3000") == instance_dir_name("http://localhost:3000")
    assert instance_dir_name("http://localhost:3000") != instance_dir_name("http://staging:3000")
    assert len(instance_dir_name("http://localhost:3000")) == 16


def test_is_fresh_ttl_boundary() -> None:
    assert is_fresh(1000.0, 1600.0, 600.0)  # exactly at the TTL -> still fresh
    assert not is_fresh(1000.0, 1600.1, 600.0)  # past it -> stale
    assert not is_fresh(1000.0, 1200.0, 0.0)  # ttl <= 0 disables raw caching


def test_format_cache_stats_renders_one_aggregate_line() -> None:
    assert format_cache_stats(None) == "disabled"
    quiet = format_cache_stats(FileCacheStats(file_fetches=24, file_hits=9976, file_fetch_seconds=0.4))
    assert "files: 24 fetched, 9976 hits (0.4s)" in quiet
    assert "evictions" not in quiet and "invalidations" not in quiet  # zero counters stay quiet
    loud = format_cache_stats(FileCacheStats(evictions=3, invalidations=5))
    assert "evictions: 3" in loud and "invalidations: 5" in loud


# --- store: file bytes (immutable, never expire) --------------------------------


def test_file_bytes_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_file_bytes("f.html") is None  # miss before any put
    store.put_file_bytes("f.html", b"BYTES")
    assert store.get_file_bytes("f.html") == b"BYTES"


def test_file_bytes_empty_is_a_hit_not_a_miss(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_file_bytes("empty", b"")
    assert store.get_file_bytes("empty") == b""


def test_unsafe_keys_stay_inside_the_cache_dir(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_file_bytes("../escape.html", b"X")
    store.put_raw("../escape/../S1", "en", {"title": "T"})

    assert store.get_file_bytes("../escape.html") == b"X"
    assert store.get_raw("../escape/../S1", "en") == {"title": "T"}
    assert not (tmp_path / "escape.html").exists()  # nothing escaped the root


def test_atomic_writes_leave_no_temp_files_behind(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_file_bytes("a", b"A")
    store.put_file_bytes("b", b"B")
    store.put_raw("S1", "en", {"title": "T"})
    assert [p for p in (tmp_path / "cache").rglob("*.tmp")] == []


# --- store: entity raws (TTL) ----------------------------------------------------


def test_raw_round_trip_and_language_isolation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_raw("S1", "en", {"title": "english"})
    store.put_raw("S1", "es", {"title": "spanish"})
    store.put_raw("S1", None, {"title": "default-locale"})

    assert store.get_raw("S1", "en") == {"title": "english"}
    assert store.get_raw("S1", "es") == {"title": "spanish"}
    assert store.get_raw("S1", None) == {"title": "default-locale"}
    assert store.get_raw("S1", "fr") is None  # never-cached language row


def test_raw_expires_after_ttl(tmp_path: Path) -> None:
    clock = _FakeClock()
    store = _store(tmp_path, ttl_seconds=600.0, clock=clock)
    store.put_raw("S1", "en", {"title": "T"})

    clock.now = 1600.0  # exactly the TTL boundary -> still fresh
    assert store.get_raw("S1", "en") == {"title": "T"}
    clock.now = 1600.1  # past it -> treated as a miss
    assert store.get_raw("S1", "en") is None


def test_raw_ttl_zero_disables_raw_caching_but_files_still_cache(tmp_path: Path) -> None:
    store = _store(tmp_path, ttl_seconds=0.0)
    store.put_raw("S1", "en", {"title": "T"})
    assert store.get_raw("S1", "en") is None
    store.put_file_bytes("f.html", b"BYTES")
    assert store.get_file_bytes("f.html") == b"BYTES"


def test_invalidate_entities_drops_all_languages_of_the_sid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_raw("A", "en", {"title": "a-en"})
    store.put_raw("A", "es", {"title": "a-es"})
    store.put_raw("B", "en", {"title": "b-en"})

    store.invalidate_entities(["A"])

    assert store.get_raw("A", "en") is None
    assert store.get_raw("A", "es") is None
    assert store.get_raw("B", "en") == {"title": "b-en"}  # untouched
    assert store.snapshot_stats().invalidations == 1


def test_invalidate_files_drops_only_the_named_bytes(tmp_path: Path) -> None:
    """The delete-path eviction: a deleted file's cached bytes must not survive
    its delete (a stale raw's ghost ref could otherwise re-confirm identity);
    the OTHER files' bytes and every raw entry stay untouched."""
    store = _store(tmp_path)
    store.put_file_bytes("f1", b"ONE")
    store.put_file_bytes("f2", b"TWO")
    store.put_raw("A", "en", {"title": "a"})

    store.invalidate_files(["f2", "missing"])

    assert store.get_file_bytes("f2") is None
    assert store.get_file_bytes("f1") == b"ONE"
    assert store.get_raw("A", "en") == {"title": "a"}  # raws untouched
    assert store.snapshot_stats().invalidations == 1  # only the evicted file counted


def test_caches_are_namespaced_per_instance_root(tmp_path: Path) -> None:
    left = _store(tmp_path, root=tmp_path / "left")
    right = _store(tmp_path, root=tmp_path / "right")
    left.put_raw("S1", "en", {"title": "left"})
    right.put_raw("S1", "en", {"title": "right"})

    assert left.get_raw("S1", "en") == {"title": "left"}
    assert right.get_raw("S1", "en") == {"title": "right"}


# --- store: eviction --------------------------------------------------------------


def test_eviction_trims_oldest_entries_over_cap(tmp_path: Path) -> None:
    store = _store(tmp_path, max_bytes=1200, evict_scan_interval=1)
    ancient = time.time() - 3600.0

    store.put_file_bytes("old", b"x" * 500)
    os.utime(_file_entry_path(tmp_path, "old"), (ancient, ancient))
    store.put_file_bytes("mid", b"x" * 500)
    os.utime(_file_entry_path(tmp_path, "mid"), (ancient + 100.0, ancient + 100.0))
    store.put_file_bytes("new", b"x" * 500)  # scan: 1500 > 1200 -> oldest evicted

    assert store.get_file_bytes("old") is None
    assert store.get_file_bytes("mid") == b"x" * 500
    assert store.get_file_bytes("new") == b"x" * 500
    assert store.snapshot_stats().evictions == 1


def test_eviction_scan_sweeps_expired_raws(tmp_path: Path) -> None:
    store = _store(tmp_path, ttl_seconds=600.0, evict_scan_interval=1)
    store.put_raw("S1", "en", {"title": "T"})
    entry = _raw_entry_path(tmp_path, "S1", "en")
    os.utime(entry, (time.time() - 99999.0, time.time() - 99999.0))

    store.put_file_bytes("keep", b"K")  # triggers the scan

    assert not entry.exists()  # swept by mtime age, not by the read-time TTL check
    assert store.get_file_bytes("keep") == b"K"
    assert store.snapshot_stats().evictions == 1


# --- store: stats ------------------------------------------------------------------


def test_store_implements_the_stats_and_invalidation_ports(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert isinstance(store, CacheStatsPort)
    assert isinstance(store, CacheInvalidationPort)


def test_stats_reset_and_snapshot_scoped_to_the_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_file_bytes("f", b"X")
    store.get_file_bytes("f")  # hit
    store.note_file_fetch(0.25)  # miss

    window = store.snapshot_stats()
    assert window.file_hits == 1
    assert window.file_fetches == 1
    assert window.file_fetch_seconds == 0.25

    store.reset_stats()
    assert store.snapshot_stats() == FileCacheStats()


# --- decorator: file bytes ----------------------------------------------------------


def test_file_bytes_miss_then_hit(tmp_path: Path) -> None:
    inner = _InMemoryFileRepo({"doc.html": b"<html></html>"})
    repo = CachedFileRepository(inner, _store(tmp_path))

    first = asyncio.run(repo.get_file_bytes("doc.html"))
    second = asyncio.run(repo.get_file_bytes("doc.html"))

    assert first == second == b"<html></html>"
    assert inner.get_calls == ["doc.html"]  # the second read came from the cache


def test_file_bytes_none_is_never_cached(tmp_path: Path) -> None:
    inner = _InMemoryFileRepo({})  # every filename is absent
    repo = CachedFileRepository(inner, _store(tmp_path))

    assert asyncio.run(repo.get_file_bytes("missing.html")) is None
    assert asyncio.run(repo.get_file_bytes("missing.html")) is None
    assert len(inner.get_calls) == 2  # a transient non-200 must not poison the key


def test_file_bytes_empty_is_a_hit(tmp_path: Path) -> None:
    inner = _InMemoryFileRepo({"empty": b""})
    repo = CachedFileRepository(inner, _store(tmp_path))

    assert asyncio.run(repo.get_file_bytes("empty")) == b""
    assert asyncio.run(repo.get_file_bytes("empty")) == b""
    assert len(inner.get_calls) == 1  # b"" is data, not a miss


def test_uploads_delegate_untouched(tmp_path: Path) -> None:
    inner = _InMemoryFileRepo({})
    repo = CachedFileRepository(inner, _store(tmp_path))

    assert asyncio.run(repo.upload_document(b"d", "S1", "en", "t.pdf", "application/pdf"))
    assert asyncio.run(repo.upload_attachment(b"a", "S1", "es", "t.txt", "text/plain"))
    assert [u[0] for u in inner.uploads] == ["document", "attachment"]


def test_decorator_accounts_fetches_and_hits(tmp_path: Path) -> None:
    inner = _InMemoryFileRepo({"f": b"X"})
    store = _store(tmp_path)
    repo = CachedFileRepository(inner, store)

    asyncio.run(repo.get_file_bytes("f"))  # miss -> real fetch
    asyncio.run(repo.get_file_bytes("f"))  # hit

    stats = store.snapshot_stats()
    assert stats.file_fetches == 1
    assert stats.file_hits == 1
    assert stats.file_fetch_seconds >= 0.0


# --- decorator: entity raws -----------------------------------------------------------


def test_raw_hits_within_ttl_and_refetches_after_expiry(tmp_path: Path) -> None:
    clock = _FakeClock()
    inner = _InMemoryEntityRepo({"S1": {"sharedId": "S1", "title": "T"}})
    repo = CachedEntityRepository(inner, _store(tmp_path, ttl_seconds=600.0, clock=clock))

    asyncio.run(repo.get_raw_by_shared_id("S1", "en"))
    clock.now += 599.0
    asyncio.run(repo.get_raw_by_shared_id("S1", "en"))
    assert len(inner.get_calls) == 1  # still fresh at the boundary

    clock.now += 2.0  # past the TTL
    asyncio.run(repo.get_raw_by_shared_id("S1", "en"))
    assert len(inner.get_calls) == 2  # expired -> refetched (and re-cached)


def test_save_raw_invalidates_cached_raw(tmp_path: Path) -> None:
    inner = _InMemoryEntityRepo({"S1": {"sharedId": "S1", "title": "old"}})
    repo = CachedEntityRepository(inner, _store(tmp_path))

    assert asyncio.run(repo.get_raw_by_shared_id("S1", "en"))["title"] == "old"

    # An edit that bypasses our ports (a human in Uwazi) is invisible inside
    # the TTL window — the documented Phase-2 trade-off, pinned here.
    inner._raws["S1"] = {"sharedId": "S1", "title": "edited"}
    assert asyncio.run(repo.get_raw_by_shared_id("S1", "en"))["title"] == "old"

    # Our OWN write invalidates immediately.
    asyncio.run(repo.save_raw({"sharedId": "S1", "title": "new"}))
    assert asyncio.run(repo.get_raw_by_shared_id("S1", "en"))["title"] == "new"
    assert len(inner.get_calls) == 2


def test_delete_by_shared_id_invalidates_cached_raw(tmp_path: Path) -> None:
    inner = _InMemoryEntityRepo({"S1": {"sharedId": "S1", "title": "T"}})
    repo = CachedEntityRepository(inner, _store(tmp_path))
    asyncio.run(repo.get_raw_by_shared_id("S1", "en"))  # cache fill

    asyncio.run(repo.delete_by_shared_id("S1"))

    with pytest.raises(RuntimeError, match="Entity not found"):
        asyncio.run(repo.get_raw_by_shared_id("S1", "en"))
    assert len(inner.get_calls) == 2  # refetched after invalidation, not served stale


def test_get_raw_by_internal_id_is_not_cached(tmp_path: Path) -> None:
    inner = _InMemoryEntityRepo({})
    repo = CachedEntityRepository(inner, _store(tmp_path))

    asyncio.run(repo.get_raw_by_internal_id("i1"))
    asyncio.run(repo.get_raw_by_internal_id("i1"))
    assert inner.internal_get_calls == ["i1", "i1"]


def test_failed_raw_fetch_stores_nothing(tmp_path: Path) -> None:
    inner = _InMemoryEntityRepo({})  # every get raises (entity not found)
    repo = CachedEntityRepository(inner, _store(tmp_path))

    with pytest.raises(RuntimeError, match="Entity not found"):
        asyncio.run(repo.get_raw_by_shared_id("missing", "en"))

    assert not (tmp_path / "cache" / "entities").exists()  # nothing was written
