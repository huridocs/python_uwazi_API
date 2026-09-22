"""JSON snapshot of the document cache, so it survives container restarts.

The live cache lives in process memory (``PropertyFillerService._documents``)
and is only refilled by ``Connect & Refresh``; this file gives it a durable
copy under ``data/document_cache/`` — the same persistent ``admin_agent_data``
volume that already holds the PDF byte cache. Writes are atomic (temp +
``os.replace``); reads treat a missing/corrupt/mismatched file as a miss.

One snapshot per (instance, template, language) so switching templates or
languages never serves another configuration's rows.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

from loguru import logger

from uwazi_property_filler.configuration import DATA_DIR, INSTANCE_KEY
from uwazi_property_filler.domain.document_cache import DocumentCache

SNAPSHOT_DIR: Path = DATA_DIR / "document_cache"

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def snapshot_path(template: str, language: str) -> Path:
    """Snapshot file for one instance/template/language combination."""
    key = "_".join((INSTANCE_KEY, _SAFE_RE.sub("_", template), _SAFE_RE.sub("_", language)))
    return SNAPSHOT_DIR / f"{key}.json"


def load(template: str, language: str) -> DocumentCache | None:
    """Snapshot for this template/language, or ``None`` on any miss."""
    path = snapshot_path(template, language)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    documents = DocumentCache.from_json(text)
    if documents is None:
        logger.warning("Document cache snapshot {} is corrupt; ignoring it", path)
        return None
    if documents.template != template or documents.language != language:
        logger.warning("Document cache snapshot {} belongs to another template/language; ignoring it", path)
        return None
    return documents


def save(documents: DocumentCache) -> None:
    """Atomically replace this cache's snapshot."""
    path = snapshot_path(documents.template, documents.language)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(SNAPSHOT_DIR), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(documents.to_json())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
