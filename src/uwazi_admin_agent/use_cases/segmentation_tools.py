"""Read-only segmentation tools for the script-generation agent (§ segmentation).

The generation agent cannot see primary documents: ``query_entities`` returns
validated models that strip ``documents``/``attachments``. When the target
values live in a document's *structured text* (the paragraphs Uwazi's
segmentation extracts from a PDF), the agent needs a window onto those
paragraphs — not the raw PDF bytes (``peek_file_text`` decodes bytes as text and
only makes sense for HTML/text supporting files). These two tools give it that
window:

- ``get_segmentation``: fetch one file's segmentation by its ``_id`` (the
  ``file_id`` that ``peek_entity_files`` returns for kind=document entries) and
  render the paragraphs as page-ordered text.
- ``get_segmentation_by_entity``: resolve an entity's primary document for a
  language and return its segmentation, without the caller knowing the file id.

Both are read-only over the raw ``segmentation_repository`` port wired on
:class:`AdminAgentDeps` (set in ``build_runtime``). A missing port, a missing
segmentation, or a network error degrade to an error STRING (never raise — a
tool error is LLM-visible context, not a crash).
"""

from __future__ import annotations

from pydantic_ai import RunContext

from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.peek_file_tools import _truncate_peek
from uwazi_api.domain.segmentation import Segmentation


def format_segmentation(seg: Segmentation) -> str:
    """Render a :class:`Segmentation` as page-ordered text (pure; unit-testable).

    Output is a header line (filename, status, page count, paragraph count)
    followed by one line per paragraph — ``[page N] text`` — ordered by
    ``(page_number, top, left)`` (natural reading order). The page count is the
    highest paragraph ``page_number`` (pages are 1-indexed, so the max equals the
    total number of pages that carry text). Oversized output is truncated
    head-and-tail via :func:`~uwazi_admin_agent.use_cases.peek_file_tools._truncate_peek`
    so a long document stays within the LLM context.
    """
    page_count = max((p.page_number for p in seg.paragraphs), default=0)
    header = f"file: {seg.filename} (status={seg.status}, pages={page_count}, paragraphs={len(seg.paragraphs)})"
    ordered = sorted(seg.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
    body = "\n".join(f"[page {p.page_number}] {p.text}" for p in ordered)
    text = f"{header}\n{body}" if body else header
    return _truncate_peek(text)


async def get_segmentation(ctx: RunContext[AdminAgentDeps], file_id: str) -> str:
    """Fetch one file's segmentation by its ``_id`` and return its paragraphs as
    page-ordered text. ``file_id`` is the document ``_id`` from
    ``peek_entity_files`` (kind=document). Returns an error string when the
    segmentation is unavailable."""
    deps: AdminAgentDeps = ctx.deps
    if deps.segmentation_repository is None:
        return "Error: segmentation_repository is not wired; cannot read segmentation."
    if not file_id:
        return "Error: file_id must be a non-empty document _id (from peek_entity_files)."
    try:
        seg = await deps.segmentation_repository.get_by_file_id(file_id)
    except Exception as exc:  # noqa: BLE001 - degrade to an error string, never crash the agent turn
        return f"Error: segmentation fetch failed for {file_id}: {type(exc).__name__}: {exc}"
    return format_segmentation(seg)


async def get_segmentation_by_entity(ctx: RunContext[AdminAgentDeps], shared_id: str, language: str = "en") -> str:
    """Resolve an entity's primary document for ``language`` and return its
    segmentation as page-ordered text. ``shared_id`` is the entity's sharedId;
    ``language`` is the row locale (ISO 639-1, default ``en``). Returns an error
    string when the entity has no document for that language or segmentation is
    unavailable."""
    deps: AdminAgentDeps = ctx.deps
    if deps.segmentation_repository is None:
        return "Error: segmentation_repository is not wired; cannot read segmentation."
    if not shared_id:
        return "Error: shared_id must be a non-empty entity sharedId."
    try:
        seg = await deps.segmentation_repository.get_by_shared_id(shared_id, language)
    except Exception as exc:  # noqa: BLE001 - degrade to an error string
        return f"Error: segmentation fetch failed for {shared_id} ({language}): {type(exc).__name__}: {exc}"
    return format_segmentation(seg)
