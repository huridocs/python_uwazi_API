"""Static suggestion sources backed by JSON Lines files.

Every ``*.jsonl`` file under a suggestions directory is one source, identified
by its file stem (a per-row ``source`` field overrides it). This makes adding
another model's predictions a matter of dropping another ``*.jsonl`` file in
the directory.

Several directories are scanned: the packaged one (shipped with the code) and
the runtime ones (operator-owned). The directories are read in order, so an
earlier directory wins when two files share a stem.

A row carries:

* ``id`` — ``<property_id>:<shared_id>``; the shared id after the last colon
  matches the Uwazi entity (a plain shared id is accepted too),
* ``predicted_labels`` — the suggested values (a list or a single string),
* optional ``property_name`` (defaults to the configured fill property),
* optional ``confidence`` (defaults to ``1.0``).

Malformed lines are logged and skipped: one bad row never hides the rest.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from loguru import logger

from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.ports.suggestion_source_port import SuggestionSourcePort


def shared_id_from_id(value: Any) -> str:
    """Extract the entity shared id from ``<property_id>:<shared_id>``.

    The id prefix identifies the property/experiment; the suffix after the last
    colon is the Uwazi shared id. A value without a colon is returned as-is.
    """
    text = str(value or "").strip()
    return text.rsplit(":", 1)[-1].strip() if text else ""


def _labels(value: Any) -> list[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list):
        return []
    labels: list[str] = []
    for item in items:
        text = str(item).strip() if item is not None else ""
        if text and text not in labels:
            labels.append(text)
    return labels


def _confidence(value: Any) -> float:
    try:
        return float(value) if value is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def parse_suggestion_line(
    record: Mapping[str, Any],
    *,
    source: str,
    default_property: str,
) -> tuple[str, Suggestion] | None:
    """Parse one JSONL row into ``(shared_id, suggestion)``, or ``None``."""
    shared_id = shared_id_from_id(record.get("id") or record.get("shared_id"))
    labels = _labels(record.get("predicted_labels"))
    if not shared_id or not labels:
        return None
    source_id = str(record.get("source") or source).strip() or source
    property_name = str(record.get("property_name") or default_property).strip() or default_property
    return shared_id, Suggestion(
        property_name=property_name,
        value=labels,
        confidence=_confidence(record.get("confidence")),
        source_extension_id=source_id,
    )


class JsonlSuggestionSource(SuggestionSourcePort):
    """Read every ``*.jsonl`` file in the given directories as suggestion sources."""

    def __init__(self, directories: Path | Iterable[Path], default_property: str):
        if isinstance(directories, Path):
            self._directories = [directories]
        else:
            self._directories = list(directories)
        self._default_property = default_property
        # Cached parse keyed by (path, mtime, size); a metadata-only change
        # (new file, replaced file) invalidates it automatically.
        self._cache_signature: tuple[tuple[str, int, int], ...] | None = None
        self._cache: list[tuple[str, Suggestion]] = []

    def files(self) -> list[Path]:
        """Every ``*.jsonl`` file of every directory, in directory order.

        Files from every directory are read; a file's stem is its source id, so
        two files sharing a stem in different directories contribute rows
        under the same source id.
        """
        found: list[Path] = []
        for directory in self._directories:
            if not directory.exists():
                continue
            found.extend(sorted(directory.glob("*.jsonl")))
        return found

    def _signature(self, files: list[Path]) -> tuple[tuple[str, int, int], ...]:
        signature: list[tuple[str, int, int]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def _load(self) -> list[tuple[str, Suggestion]]:
        files = self.files()
        signature = self._signature(files)
        if signature == self._cache_signature:
            return self._cache
        parsed: list[tuple[str, Suggestion]] = []
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                logger.warning("Could not read suggestions source {}: {}", path, exc)
                continue
            for line_no, line in enumerate(lines, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping malformed suggestion in {}:{}: {}", path.name, line_no, exc)
                    continue
                if not isinstance(record, dict):
                    logger.warning("Skipping non-object suggestion in {}:{}", path.name, line_no)
                    continue
                item = parse_suggestion_line(
                    record,
                    source=path.stem,
                    default_property=self._default_property,
                )
                if item is not None:
                    parsed.append(item)
        self._cache_signature = signature
        self._cache = parsed
        return parsed

    def _list_for_sync(self, shared_id: str) -> list[Suggestion]:
        return [suggestion for sid, suggestion in self._load() if sid == shared_id]

    def _shared_ids_sync(self) -> set[str]:
        return {shared_id for shared_id, _ in self._load()}

    async def list_for(self, shared_id: str) -> list[Suggestion]:
        return await asyncio.to_thread(self._list_for_sync, shared_id)

    async def shared_ids(self) -> set[str]:
        return await asyncio.to_thread(self._shared_ids_sync)
