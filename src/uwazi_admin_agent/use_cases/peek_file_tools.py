"""Generation-time peek tools: read-only file access for HTML sampling.

The script-generation agent cannot see files: ``query_entities`` returns
validated models that strip ``documents``/``attachments``. Before authoring the
``extract`` function, the extractor subagent needs to LOOK at real supporting
files (read-only, no mutation) to learn where the target values live. These two
tools give it that window:

- ``peek_entity_files``: list the uploaded file refs (documents + uploaded
  attachments) for a batch of entities — filenames, kinds, content types.
- ``peek_file_text``: fetch one file's bytes and return them decoded as text
  (``utf-8``/``replace``), truncated to ``MAX_PEEK_CHARS`` keeping head AND tail
  with a ``[truncated]`` marker — HTML supporting files can be huge and the LLM
  context is not; pagination/footer tables often live near the file's end.

Both are read-only over the raw repository ports wired on
:class:`AdminAgentDeps` (``entity_repository`` / ``file_repository``, set in
``build_runtime``). Missing ports / entities degrade to error STRINGS (never
raise — a tool error is LLM-visible context, not a crash).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import RunContext

from uwazi_admin_agent.configuration import MAX_PEEK_CHARS

# Tail window kept when truncating: pagination/footer tables live near the end.
_PEEK_TAIL_CHARS: int = 50_000

from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps

# Cap on entities per peek_entity_files call: bounds the JSON payload size.
MAX_PEEK_IDS: int = 50


async def peek_entity_files(ctx: RunContext[AdminAgentDeps], shared_ids: list[str]) -> str:
    """List the uploaded supporting files (documents + uploaded attachments) for
    each entity. Returns JSON {shared_id: [file dicts]} — each file dict carries
    file_id, kind, filename, originalname, language, content_type. Missing
    entities get an {"error": ...} entry."""
    deps: AdminAgentDeps = ctx.deps
    if deps.entity_repository is None:
        return "Error: entity_repository is not wired; cannot peek entity files."
    if not shared_ids:
        return "Error: shared_ids must be a non-empty list of entity shared_ids."
    if len(shared_ids) > MAX_PEEK_IDS:
        return f"Error: at most {MAX_PEEK_IDS} shared_ids per call (got {len(shared_ids)})."
    out: dict[str, Any] = {}
    for sid in shared_ids:
        try:
            raw = await deps.entity_repository.get_raw_by_shared_id(sid, "en")
            out[sid] = [ref.model_dump() for ref in extract_file_refs(raw)]
        except Exception as exc:  # noqa: BLE001 - degrade to an error entry, never crash the agent turn
            out[sid] = {"error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(out, ensure_ascii=False)


async def peek_file_text(ctx: RunContext[AdminAgentDeps], shared_id: str, filename: str) -> str:
    """Fetch one supporting file's bytes by storage filename and return them
    decoded as text (utf-8, errors='replace'), truncated to MAX_PEEK_CHARS keeping
    head and tail with a '[truncated]' marker. ``shared_id`` is accepted for
    logging/traceability only."""
    deps: AdminAgentDeps = ctx.deps
    del shared_id  # traceability only: the storage filename is the fetch key
    if deps.file_repository is None:
        return "Error: file_repository is not wired; cannot peek file text."
    if not filename:
        return "Error: filename must be a non-empty storage filename (from peek_entity_files)."
    try:
        data = await deps.file_repository.get_file_bytes(filename)
    except Exception as exc:  # noqa: BLE001 - degrade to an error string
        return f"Error: fetch failed for {filename}: {type(exc).__name__}: {exc}"
    if data is None:
        return f"Error: file not found: {filename}"
    text = data.decode("utf-8", errors="replace")
    return _truncate_peek(text)


def _truncate_peek(text: str) -> str:
    """Head+tail truncation to MAX_PEEK_CHARS (pure; unit-testable offline)."""
    if len(text) > MAX_PEEK_CHARS:
        head, tail = MAX_PEEK_CHARS - _PEEK_TAIL_CHARS, _PEEK_TAIL_CHARS
        return text[:head] + "\n[...middle truncated...]\n" + text[-tail:] + "[truncated]"
    return text
