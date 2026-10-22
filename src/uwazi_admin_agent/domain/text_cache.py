"""Stable cache keys for expensive text results (pure).

The LLM and translation caches must never return an answer produced for a
different request. A key therefore hashes every input that can change the
answer:

- the cache schema version (bump it when the stored shape or key semantics
  change);
- the request kind (complete vs. batch vs. translation);
- the runtime identity (model / adapter version);
- the full instruction and text (or the whole ordered batch);
- the source and target language for a translation.

The result is a fixed-length hexadecimal digest, safe as a filename on every
filesystem and cheap to compare.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Bump when the meaning of a key or the stored value changes. Old entries then
# become unreachable (not deleted) and are reaped by the cache's normal LRU
# eviction.
TEXT_CACHE_VERSION: str = "1"


def _digest(payload: dict[str, Any]) -> str:
    """A stable SHA-256 digest of one canonical JSON payload (pure)."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def llm_complete_cache_key(*, namespace: str, instruction: str, text: str) -> str:
    """The key for one ``llm_complete`` call (pure)."""
    return _digest(
        {
            "version": TEXT_CACHE_VERSION,
            "kind": "llm_complete",
            "namespace": namespace,
            "instruction": instruction,
            "text": text,
        }
    )


def llm_map_cache_key(*, namespace: str, instruction: str, items: list[str]) -> str:
    """The key for one ``llm_map`` batch (the WHOLE ordered batch, pure)."""
    return _digest(
        {
            "version": TEXT_CACHE_VERSION,
            "kind": "llm_map",
            "namespace": namespace,
            "instruction": instruction,
            "items": list(items),
        }
    )


def translation_cache_key(*, text: str, language_from: str, language_to: str) -> str:
    """The key for one translation request (pure)."""
    return _digest(
        {
            "version": TEXT_CACHE_VERSION,
            "kind": "translation",
            "text": text,
            "language_from": language_from,
            "language_to": language_to,
        }
    )


__all__ = [
    "TEXT_CACHE_VERSION",
    "llm_complete_cache_key",
    "llm_map_cache_key",
    "translation_cache_key",
]
