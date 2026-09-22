"""Bytes-only on-disk cache for PDFs, mirroring the admin agent's file cache.

Layout: ``PDF_CACHE_DIR/files/<safe_name(filename)>``. Storage filenames are
minted fresh per upload (``<timestamp><random>.<ext>``) and are unique, so byte
entries are immutable and cache forever until evicted. Writes are atomic
(temp + ``os.replace``); reads treat any ``OSError`` as a miss. A max-bytes cap
with oldest-mtime eviction runs amortized on ``put``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import tempfile
from pathlib import Path

from uwazi_property_filler.configuration import PDF_CACHE_DIR, PDF_CACHE_MAX_BYTES
from uwazi_property_filler.ports.pdf_cache_port import PdfCachePort

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# Evict down to this fraction of the cap so the next put does not re-scan.
_EVICT_HEADROOM = 0.9


def safe_name(filename: str) -> str:
    """Map a storage filename to a safe on-disk key (no path separators)."""
    return _SAFE_RE.sub("_", filename)


class PdfCacheStore(PdfCachePort):
    def __init__(
        self,
        root: Path = PDF_CACHE_DIR,
        max_bytes: int = PDF_CACHE_MAX_BYTES,
        evict_scan_interval: int = 256,
    ):
        self._root = root
        self._files_dir = root / "files"
        self._max_bytes = max_bytes
        self._evict_scan_interval = evict_scan_interval
        self._puts_since_scan = 0
        self._files_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, filename: str) -> Path:
        return self._files_dir / safe_name(filename)

    async def get(self, filename: str) -> bytes | None:
        return await asyncio.to_thread(self._get_sync, filename)

    def _get_sync(self, filename: str) -> bytes | None:
        try:
            return self._path(filename).read_bytes()
        except OSError:
            return None

    async def put(self, filename: str, data: bytes) -> None:
        await asyncio.to_thread(self._put_sync, filename, data)

    def _put_sync(self, filename: str, data: bytes) -> None:
        target = self._path(filename)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._files_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp_path, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        self._puts_since_scan += 1
        if self._puts_since_scan >= self._evict_scan_interval:
            self._puts_since_scan = 0
            self._evict()

    async def clear(self) -> None:
        await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        for entry in self._files_dir.iterdir():
            try:
                entry.unlink()
            except OSError:
                pass

    def _evict(self) -> None:
        entries = [p for p in self._files_dir.iterdir() if p.is_file()]
        total = sum(_safe_size(p) for p in entries)
        if total <= self._max_bytes:
            return
        target = int(self._max_bytes * _EVICT_HEADROOM)
        entries.sort(key=lambda p: p.stat().st_mtime)
        for p in entries:
            if total <= target:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
            except OSError:
                continue


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
