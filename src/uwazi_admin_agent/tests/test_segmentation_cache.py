"""Isolated unit tests for the segmentation cache (store + decorator + parallel).

Per ``AGENTS.md``: no mocks/stubs, no network, no running Uwazi instance. The
on-disk ``FileCacheStore`` segmentation regime is exercised against a tmp dir
(the ``test_file_cache.py`` precedent); the ``CachedSegmentationRepository``
decorator is driven against a tiny REAL in-memory ``SegmentationRepositoryPort``;
the ``get_segmentation_parallel`` helper is built from the REAL
``build_parallel_read_helpers`` factory and bound through the REAL namespace
builders. Everything here is deterministic and offline.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any, override

import pytest

from uwazi_admin_agent.adapters.cached_segmentation_repository import CachedSegmentationRepository
from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.domain.file_cache import safe_cache_name
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.use_cases.parallel_executor import ParallelExecutor
from uwazi_admin_agent.use_cases.parallel_script_helpers import build_parallel_read_helpers
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    build_dry_run_namespace,
    build_exec_namespace,
    build_real_exec_namespace,
)
from uwazi_admin_agent.use_cases.throttle_controller import ThrottleController
from uwazi_api.domain.exceptions import SegmentationNotFoundError
from uwazi_api.domain.segmentation import Paragraph, Segmentation


def _seg(status: str = "ready", paragraphs: list[Paragraph] | None = None) -> Segmentation:
    return Segmentation(
        id="seg-1",
        file_id="file-1",
        document_id="doc-1",
        filename="doc.pdf",
        status=status,
        paragraphs=paragraphs if paragraphs is not None else [_para(1, "one")],
    )


def _para(page_number: int, text: str) -> Paragraph:
    return Paragraph(left=0.0, top=0.0, width=10.0, height=10.0, pageNumber=page_number, text=text, type="paragraph")


class _InMemorySegRepo(SegmentationRepositoryPort):
    """A dict-backed SegmentationRepositoryPort that counts its calls."""

    def __init__(
        self, by_file_id: dict[str, Segmentation] | None = None, by_shared_id: dict[str, Segmentation] | None = None
    ) -> None:
        self._by_file_id = by_file_id if by_file_id is not None else {}
        self._by_shared_id = by_shared_id if by_shared_id is not None else {}
        self.get_calls: list[str] = []
        self.shared_get_calls: list[tuple[str, str]] = []

    @override
    async def get_by_file_id(self, file_id: str) -> Segmentation:
        self.get_calls.append(file_id)
        if file_id not in self._by_file_id:
            raise SegmentationNotFoundError(f"no segmentation for {file_id}")
        return self._by_file_id[file_id]

    @override
    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        self.shared_get_calls.append((shared_id, language))
        if shared_id not in self._by_shared_id:
            raise SegmentationNotFoundError(f"no segmentation for {shared_id}")
        return self._by_shared_id[shared_id]


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


def _seg_path(base: Path, file_id: str) -> Path:
    """The documented on-disk layout path for one cached segmentation entry."""
    return base / "cache" / "segmentations" / f"{safe_cache_name(file_id)}.json"


# --- store: segmentations (immutable-forever, keyed by file_id) --------------------


def test_segmentation_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.get_segmentation("file-1") is None  # miss before any put

    store.put_segmentation("file-1", _seg())

    got = store.get_segmentation("file-1")
    assert got is not None
    assert got.status == "ready"
    assert got.filename == "doc.pdf"
    assert [p.text for p in got.paragraphs] == ["one"]


def test_segmentation_key_is_file_id_not_filename(tmp_path: Path) -> None:
    """Entries are keyed by the document ``_id``, so two file_ids never collide
    even when they would share a storage filename."""
    store = _store(tmp_path)
    store.put_segmentation("file-a", _seg())
    store.put_segmentation("file-b", _seg())

    assert store.get_segmentation("file-a") is not None
    assert store.get_segmentation("file-b") is not None


def test_invalidate_segmentations_drops_only_the_named_file_ids(tmp_path: Path) -> None:
    """The delete-path eviction: a deleted file's cached segmentation must not
    survive its delete; the OTHER file's segmentation and every raw stay put."""
    store = _store(tmp_path)
    store.put_segmentation("file-a", _seg())
    store.put_segmentation("file-b", _seg())
    store.put_raw("S1", "en", {"title": "t"})

    store.invalidate_segmentations(["file-b", "missing"])

    assert store.get_segmentation("file-b") is None
    assert store.get_segmentation("file-a") is not None
    assert store.get_raw("S1", "en") == {"title": "t"}  # raws untouched
    assert store.snapshot_stats().invalidations == 1  # only the evicted entry counted


def test_segmentation_stats_counters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_segmentation("file-1", _seg())
    store.get_segmentation("file-1")  # hit
    store.note_segmentation_fetch(0.25)  # miss

    stats = store.snapshot_stats()
    assert stats.seg_hits == 1
    assert stats.seg_fetches == 1
    assert stats.seg_fetch_seconds == 0.25


def test_clear_removes_segmentations_too(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.put_file_bytes("f", b"X")
    store.put_raw("S1", "en", {"title": "t"})
    store.put_segmentation("file-1", _seg())

    assert store.clear() == 3
    assert store.get_segmentation("file-1") is None


def test_segmentation_entries_are_not_swept_as_expired_raws(tmp_path: Path) -> None:
    """Ready segmentations are immutable-forever: the eviction scan sweeps
    expired RAWS by mtime, but a segmentation JSON (also ``.json``) lives under
    ``segmentations/`` and must survive the same scan."""
    store = _store(tmp_path, ttl_seconds=600.0, evict_scan_interval=1)
    ancient = time.time() - 99999.0

    store.put_segmentation("file-1", _seg())
    os.utime(_seg_path(tmp_path, "file-1"), (ancient, ancient))

    store.put_raw("S1", "en", {"title": "t"})
    raw_path = tmp_path / "cache" / "entities" / safe_cache_name("S1") / f"{safe_cache_name('en')}.json"
    os.utime(raw_path, (ancient, ancient))

    store.put_file_bytes("keep", b"K")  # triggers the scan

    assert store.get_segmentation("file-1") is not None  # survived (never expires)
    assert store.get_raw("S1", "en") is None  # swept by mtime age
    assert store.get_file_bytes("keep") == b"K"


# --- decorator: ready-only caching ---------------------------------------------


def test_ready_segmentation_miss_then_hit(tmp_path: Path) -> None:
    inner = _InMemorySegRepo(by_file_id={"file-1": _seg()})
    repo = CachedSegmentationRepository(inner, _store(tmp_path))

    first = asyncio.run(repo.get_by_file_id("file-1"))
    second = asyncio.run(repo.get_by_file_id("file-1"))

    assert first.status == second.status == "ready"
    assert inner.get_calls == ["file-1"]  # the second read came from the cache


def test_processing_segmentation_is_never_cached(tmp_path: Path) -> None:
    """A non-ready result must re-try on the next read (the status transitions
    processing -> ready), so it is never pinned."""
    seg = _seg(status="processing")
    inner = _InMemorySegRepo(by_file_id={"file-1": seg})
    repo = CachedSegmentationRepository(inner, _store(tmp_path))

    assert asyncio.run(repo.get_by_file_id("file-1")).status == "processing"
    assert asyncio.run(repo.get_by_file_id("file-1")).status == "processing"
    assert len(inner.get_calls) == 2  # never cached

    # Once the server flips it to ready, the next read caches and serves hits.
    inner._by_file_id["file-1"] = _seg(status="ready")
    assert asyncio.run(repo.get_by_file_id("file-1")).status == "ready"
    assert asyncio.run(repo.get_by_file_id("file-1")).status == "ready"
    assert len(inner.get_calls) == 3  # the ready result was cached


def test_notfound_is_never_cached(tmp_path: Path) -> None:
    """A missing segmentation (SegmentationNotFoundError) must not poison the
    key — every read re-tries the instance."""
    inner = _InMemorySegRepo()  # no segmentations
    repo = CachedSegmentationRepository(inner, _store(tmp_path))

    with pytest.raises(SegmentationNotFoundError):
        asyncio.run(repo.get_by_file_id("missing"))
    with pytest.raises(SegmentationNotFoundError):
        asyncio.run(repo.get_by_file_id("missing"))

    assert len(inner.get_calls) == 2
    assert not (tmp_path / "cache" / "segmentations").exists()  # nothing written


def test_decorator_accounts_fetches_and_hits(tmp_path: Path) -> None:
    inner = _InMemorySegRepo(by_file_id={"file-1": _seg()})
    store = _store(tmp_path)
    repo = CachedSegmentationRepository(inner, store)

    asyncio.run(repo.get_by_file_id("file-1"))  # miss -> real fetch
    asyncio.run(repo.get_by_file_id("file-1"))  # hit

    stats = store.snapshot_stats()
    assert stats.seg_fetches == 1
    assert stats.seg_hits == 1
    assert stats.seg_fetch_seconds >= 0.0


def test_get_by_shared_id_delegates_uncached(tmp_path: Path) -> None:
    """The shared_id -> file_id resolution is a runtime JOIN inside uwazi_api,
    so the decorator passes it through without caching."""
    inner = _InMemorySegRepo(by_shared_id={"S1": _seg()})
    repo = CachedSegmentationRepository(inner, _store(tmp_path))

    asyncio.run(repo.get_by_shared_id("S1", "en"))
    asyncio.run(repo.get_by_shared_id("S1", "en"))

    assert inner.shared_get_calls == [("S1", "en"), ("S1", "en")]  # not cached
    assert not (tmp_path / "cache" / "segmentations").exists()


# --- parallel helper ------------------------------------------------------------


def test_get_segmentation_parallel_fans_out_and_maps_none(tmp_path: Path) -> None:
    """The bulk read: one task per shared_id, results keyed by shared_id in
    input order, ``None`` for an entity with no ready segmentation."""
    inner = _InMemorySegRepo(
        by_shared_id={"S1": _seg(), "S3": _seg()},
    )
    executor = ParallelExecutor(ThrottleController())
    helpers = build_parallel_read_helpers(None, None, "en", executor, segmentation_repository=inner)

    result = helpers["get_segmentation_parallel"](["S1", "S2", "S3"])

    assert list(result.keys()) == ["S1", "S2", "S3"]
    assert result["S1"]["status"] == "ready"
    assert result["S1"]["filename"] == "doc.pdf"
    assert result["S2"] is None  # no segmentation -> None, like the single helper
    assert result["S3"]["status"] == "ready"


def test_get_segmentation_parallel_empty_returns_empty(tmp_path: Path) -> None:
    executor = ParallelExecutor(ThrottleController())
    helpers = build_parallel_read_helpers(None, None, "en", executor, segmentation_repository=_InMemorySegRepo())

    assert helpers["get_segmentation_parallel"]([]) == {}


def test_get_segmentation_parallel_unwired_raises(tmp_path: Path) -> None:
    executor = ParallelExecutor(ThrottleController())
    helpers = build_parallel_read_helpers(None, None, "en", executor)  # no segmentation_repository

    with pytest.raises(RuntimeError, match="get_segmentation_parallel requires a wired"):
        helpers["get_segmentation_parallel"](["S1"])


def test_segmentation_parallel_bound_in_real_and_dry_run_not_dummy() -> None:
    """The parallel name binds in real + dry-run (real reads), but NOT the dummy
    namespace (which keeps ``get_segmentation`` a no-op)."""
    repo = _InMemorySegRepo()

    real = build_real_exec_namespace(
        entity_api=None,  # type: ignore[arg-type]
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        intercept=_StubIntercept(),
        tool_cache=None,
        default_language="en",
        segmentation_repository=repo,
    )
    dry = build_dry_run_namespace(
        entity_api=None,
        loop=asyncio.new_event_loop(),
        file_repository=None,
        default_language="en",
        dry_run_records=[],
        segmentation_repository=repo,
    )
    dummy = build_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=asyncio.new_event_loop(),
        scope=set(),
        dummy_entities=[],
        tool_cache=None,
        default_language="en",
    )

    assert "get_segmentation_parallel" in real
    assert "get_segmentation_parallel" in dry
    assert "get_segmentation_parallel" not in dummy


class _StubIntercept:
    """Minimal real stand-in for the BackupIntercept seam (no ports involved)."""

    def decorate(self, crud: tuple) -> dict:
        names = (
            "create_entities",
            "update_entities",
            "delete_entities",
            "publish_entities",
            "unpublish_entities",
            "set_publish_status",
            "create_relationships",
        )
        return dict(zip(names, crud))
