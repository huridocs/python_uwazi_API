"""The ``author_html_extractor`` tool: a nested subagent that authors the pure
``def extract(html, ctx) -> dict | None`` function for HTML extraction tasks (§
extraction phase).

Why a subagent: a bulk extraction over ~5000 entities cannot hardcode one
document's layout — the value sits in a different spot per document. Instead a
nested extractor agent SAMPLES ~8-12 real HTML supporting files (via
``peek_entity_files`` / ``peek_file_text``), authors a pure
``def extract(html, ctx) -> dict | None`` with ordered fallback strategies, and
SELF-PROVES it by executing it directly (in-process, no Uwazi) over the same
literal samples. The main generation agent then embeds the returned source
VERBATIM in the emitted script — zero edits — because it was proven in this
exact sandbox (``def`` works, ``class`` does not; ``htmlextract.*`` and
``re`` are bound).

The tool itself stays model-free: the extractor :class:`pydantic_ai.Agent` is
built ONCE in :class:`GenerateScriptUseCase.__init__` (the use case owns the
``LlmPort``) and shared via ``AdminAgentDeps.extractor_agent``; the tool just
runs it under ``UsageLimits(request_limit=MAX_EXTRACTOR_LLM_CALLS)`` and
validates the emitted source with :func:`validate_extractor_source` (a pure
seam, unit-tested without mocks).
"""

from __future__ import annotations

from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, UsageLimits

from uwazi_admin_agent.configuration import MAX_EXTRACTOR_LLM_CALLS
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.extraction_benchmark_tool import benchmark_extractor
from uwazi_admin_agent.use_cases.extractor_sandbox import validate_extractor_source
from uwazi_admin_agent.use_cases.peek_file_tools import peek_entity_files, peek_file_text
from uwazi_admin_agent.use_cases.segmentation_tools import peek_segmentation, peek_segmentation_by_entity
from uwazi_admin_agent.use_cases.url_fetch_tools import peek_url_text
from uwazi_agent.use_cases.tools.get_templates_by_names import get_templates_by_names
from uwazi_agent.use_cases.tools.query_entities import query_entities

_EXTRACTOR_SYSTEM_PROMPT = """\
class ExtractorFunction(BaseModel):
Your ONLY output (via the ExtractorFunction schema) is a PURE Python function:

    def extract(html: str, ctx: dict) -> dict | None:

- NO import lines. NO class statements. (The target sandbox has no
  `__import__` and no `__build_class__`; `def` works.)
- You MAY call the pre-bound `htmlextract` helpers — they are bound in the
  emitted script's namespace too, so your function behaves identically at
  authoring time and at execute time:
      htmlextract.tables(html) -> list[table]; each table is rows of cell-text lists
      htmlextract.text(html)   -> all visible text, tags stripped, whitespace collapsed
      htmlextract.meta(html)   -> {meta name-or-property: content}
      htmlextract.title(html)  -> <title> text or ""
- Also allowed: the bound `re` module and plain str/list/dict methods, plus the
  bound `textmatch` (fuzzy label/name matching, scores 0..100) and `dateparse`
  (deterministic date detection -> "YYYY-MM-DD") namespaces. Both are bound in
  the emitted script's namespace too:
      textmatch.wratio(a, b)                 # 0..100 similarity (labels/names)
      textmatch.rank(q, choices, score_cutoff=80)   # [{"choice","score","index"}]
      textmatch.best(q, choices, score_cutoff=80)   # best hit or None
      dateparse.parse(text)                  # "YYYY-MM-DD" or None
      dateparse.find(text)                   # [{"iso","text","start","end"}]
  PREFER THEM over hand-rolled normalisation/scoring: `textmatch.wratio` for
  "is this the label/cell I want", `dateparse.parse` for a date value. Never
  write a guessed date: `dateparse.parse` returning None means "not found".
- `ctx` carries the entity's identity: a plain dict
  `{"shared_id": str, "title": str, "metadata": {prop: [values]}}` built by the
  bulk script per entity. When one HTML document holds rows/values for MULTIPLE
  entities (e.g. a table with one row per entity, distinguishable only by an
  entity property), use `ctx` to select the entry that belongs to THIS entity
  (e.g. match `ctx["title"]` or a `ctx["metadata"]` value against the row) and
  return only that entity's values. When the HTML holds one entity's data, you
  may ignore `ctx`.
- Strategy: ORDERED FALLBACKS for scattered values, because the value is NOT in
  the same spot in every document. Try in order, first hit wins:
  1. meta tag lookup (htmlextract.meta)
  2. table-label lookup (htmlextract.tables: find a label cell, take its neighbor)
  3. anchored section heading (regex for a heading, capture what follows)
  4. regex over the collapsed text (htmlextract.text)
- Return a dict of {property_name: extracted_value} when found; return None when
  nothing is found (the entity is then left untouched).
- ALSO return the passage the values were read from under the reserved key
  `"_context"` — the cell/paragraph text the operator needs to VERIFY the value,
  i.e. the matched sentence/row plus its immediate surroundings (roughly 2-3
  sentences, up to ~900 characters). Not a summary, not a bare fragment, and
  not the whole page:
      return {"document_ref": ref, "_context": cell_text}
  The generated script pops that key into the write's `context`, which the
  operator's review window shows as the evidence at the place the extraction
  happened. A missing `_context` forces them to search the document; a wrong one
  (the whole page, a different row) points at the wrong place.
- Guard the whole body with try/except returning None on odd input. Fully
  deterministic: no random, no I/O, no prints.
WORKFLOW: use the `query_entities` tool to sample ~8-12 real entities across the
target set — capture each sampled entity's title and metadata too, since they
become its `ctx` — filter to their HTML files with `htmlextract.is_html`
semantics (content_type text/html or .html/.htm originalname), fetch their texts
with `peek_file_text`, study where the target values appear, author `extract`,
then SELF-PROVE it by running your `extract` VERBATIM over the sampled literal
(html, ctx) PAIRS in your own reasoning before emitting. The pairs MUST include
at least one case where two entities share byte-identical HTML but different
`ctx` and must extract different rows/values. The pairs MUST also include at
least one NEGATIVE control: an html whose table contains rows for OTHER
entities but not the `ctx` entity — `extract` must return None (or the
strictly-matching row), never a wrong row. A false positive writes another
entity's value into this entity.
WHEN the entities carry NO supporting file but DO carry a Source Page URL (a
`link` metadata property whose value is `{"label": ..., "url": ...}` — possibly
wrapped in a one-element list): read the URL from the sampled entity's metadata
and fetch that page with `peek_url_text` instead of `peek_file_text`. The
returned page is the `html` the same `extract` function consumes, so the rest of
the workflow (author, then self-prove) is unchanged. Refine until coverage is
good, then emit ExtractorFunction with honest samples_total/samples_matched
numbers and notes about unmatched samples.
WHEN the values live in the entity's PRIMARY DOCUMENT (a PDF) rather than an
uploaded HTML file: sample with `peek_segmentation_by_entity` (page-ordered
paragraphs, each line prefixed `[page N] (Label) text`), author the same
`def extract(text, ctx)`, and use `dateparse.parse` for dates and
`textmatch.wratio` for label/paragraph matching. The `(Label)` is Uwazi's own
segment label ("Text", "Title", "Section header", "List item", "Page header",
"Page footer", "Table", ...) — filter on those literal strings if at all, never
on `section_header`/`list`/`paragraph`; a filter on an invented label yields an
empty text and no extraction.
OPTIONAL EVIDENCE: when several strategies look plausible, call
`benchmark_extractor` with 2-3 candidate sources under distinct labels and the
target property names; it scores each against the values the sampled entities
ALREADY carry. Prefer the candidate with the fewest MISMATCHES (a missing value
leaves the entity untouched; a mismatched one writes a wrong value).
When the report includes a REFINEMENT section with mismatch examples, use them
to REPAIR the failing rule inside the same `extract` (an extra fallback
strategy, a better anchor, better normalization) - never by special-casing the
listed shared_ids, titles or exact strings: those samples are a probe, and an
extractor tuned to reproduce them overfits. The tool offers at most 3
refinements per turn; when it says refinement is NOT offered (budget spent, or
too few judgeable samples), stop iterating and emit the best candidate with
honest samples_matched numbers.
"""


class ExtractorFunction(BaseModel):
    """The nested extractor agent's structured output."""

    python_code: str = Field(
        description=(
            "The complete source of a single pure `def extract(html: str, ctx: dict) -> dict | None` "
            "function. NO import lines, NO class statements."
        )
    )
    description: str = Field(description="One line: what the function extracts and from where.")
    samples_total: int = Field(description="How many real HTML samples the function was proven against.")
    samples_matched: int = Field(description="How many of those samples extraction matched.")
    unmatched_notes: str | None = Field(default=None, description="Notes on which samples/strategies failed, when any.")


async def author_html_extractor(ctx: RunContext[AdminAgentDeps], task: str) -> str:
    """Run the nested extractor subagent on ``task`` (a precise description of the
    values to extract + the target template/properties). Returns the validated
    `def extract(html, ctx)` source wrapped in a fenced block, plus its coverage
    stats. The generation agent embeds the source VERBATIM in the bulk script.

    MUST be ``async``: pydantic-ai dispatches sync tools through a worker thread
    and forbids a nested ``Agent.run_sync``/``run_stream_sync`` there
    (``check_no_nested_sync_run``), so the sync form always failed with a
    ``UserError`` that the tool reported as a string — the extractor was never
    authored at all. Delegation is the documented async shape.
    """
    deps: AdminAgentDeps = ctx.deps
    extractor_agent = deps.extractor_agent
    if extractor_agent is None:
        return "Error: extractor agent is not wired (extractor_agent is None); cannot author an extractor."
    try:
        run = await extractor_agent.run(task, deps=deps, usage_limits=UsageLimits(request_limit=MAX_EXTRACTOR_LLM_CALLS))
    except Exception as exc:  # noqa: BLE001 - surface subagent failure to the generation agent
        return f"Error: extractor subagent failed: {type(exc).__name__}: {exc}"
    output = getattr(run, "output", None)
    if not isinstance(output, ExtractorFunction):
        return f"Error: extractor subagent returned an unsupported output type: {type(output).__name__}"
    code = output.python_code
    rejection = validate_extractor_source(code)
    if rejection is not None:
        logger.warning("author_html_extractor: rejected extractor source: {}", rejection)
        return f"{rejection}\nRegenerate the extractor function and call author_html_extractor again."
    stats = f"samples: {output.samples_matched}/{output.samples_total} matched"
    if output.unmatched_notes:
        stats += f"; unmatched notes: {output.unmatched_notes}"
    logger.info("author_html_extractor: validated extractor ({})", stats)
    return f"{stats}\n\n```python\n{code}\n```"


def build_extractor_agent(model: Any) -> Agent[AdminAgentDeps, ExtractorFunction]:
    """Build the nested extractor subagent (once, from the use case's model)."""
    return Agent(
        model,
        system_prompt=_EXTRACTOR_SYSTEM_PROMPT,
        deps_type=AdminAgentDeps,
        output_type=ExtractorFunction,
        tools=[
            query_entities,
            get_templates_by_names,
            peek_entity_files,
            peek_file_text,
            peek_url_text,
            # Primary-document text: a PDF's paragraphs come from segmentation, not
            # from a supporting file, so a document-extraction task samples here.
            peek_segmentation_by_entity,
            peek_segmentation,
            # Compare competing extractor shapes over the SAME sampled entities
            # whose target properties already hold a value: evidence, not taste.
            benchmark_extractor,
        ],
    )
