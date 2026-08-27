"""The ``author_html_extractor`` tool: a nested subagent that authors the pure
``def extract(html, ctx) -> dict | None`` function for HTML extraction tasks (§
extraction phase).

Why a subagent: a bulk extraction over ~5000 entities cannot hardcode one
document's layout — the value sits in a different spot per document. Instead a
nested extractor agent SAMPLES ~8-12 real HTML supporting files (via
``peek_entity_files`` / ``peek_file_text``), authors a pure
``def extract(html, ctx) -> dict | None`` with ordered fallback strategies, and
SELF-PROVES it with ``run_validation_script`` against the same literal samples.
The main generation agent then embeds the returned source VERBATIM in the
emitted bulk script — zero edits — because it was proven in this exact sandbox
(``def`` works, ``class`` does not; ``htmlextract.*`` and ``re`` are bound).

The tool itself stays model-free: the extractor :class:`pydantic_ai.Agent` is
built ONCE in :class:`GenerateScriptUseCase.__init__` (the use case owns the
``LlmPort``) and shared via ``AdminAgentDeps.extractor_agent``; the tool just
runs it under ``UsageLimits(request_limit=MAX_EXTRACTOR_LLM_CALLS)`` and
validates the emitted source with :func:`validate_extractor_source` (a pure
seam, unit-tested without mocks).
"""

from __future__ import annotations

import ast
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field
from pydantic_ai import Agent, RunContext, UsageLimits

from uwazi_admin_agent.configuration import MAX_EXTRACTOR_LLM_CALLS
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.peek_file_tools import peek_entity_files, peek_file_text
from uwazi_admin_agent.use_cases.run_validation_script_tool import run_validation_script
from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS
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
- Also allowed: the bound `re` module and plain str/list/dict methods.
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
- Guard the whole body with try/except returning None on odd input. Fully
  deterministic: no random, no I/O, no prints.
WORKFLOW: use the `query_entities` tool to sample ~8-12 real entities across the
target set — capture each sampled entity's title and metadata too, since they
become its `ctx` — filter to their HTML files with `htmlextract.is_html`
semantics (content_type text/html or .html/.htm originalname), fetch their texts
with `peek_file_text`, study where the target values appear, author `extract`,
then SELF-PROVE it: call `run_validation_script` with a script that defines your
`extract` VERBATIM and runs it over literal (html, ctx) PAIRS (NOT bare html
strings), setting `result` to the matched/total counts (per fallback strategy if
useful). The pairs MUST include at least one case where two entities share
byte-identical HTML but different `ctx` and must extract different rows/values.
Refine until coverage is good, then emit ExtractorFunction with honest
samples_total/samples_matched numbers and notes about unmatched samples.
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


def validate_extractor_source(code: str) -> str | None:
    """Validate emitted extractor source WITHOUT mocks: parse it, reject imports
    and classes, then ``exec`` it in the script sandbox and require a callable
    two-argument ``extract``.

    Returns ``None`` when valid, otherwise a rejection message the generation
    agent can act on. Pure seam (no LLM, no I/O) — unit-tested directly.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"Rejected: extractor source does not parse ({exc.lineno}: {exc.msg})."
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return "Rejected: extractor source must contain ZERO import lines (everything is bound)."
        if isinstance(node, ast.ClassDef):
            return "Rejected: extractor source must not define classes (class does not work in the sandbox; use def)."
    namespace: dict[str, Any] = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    try:
        exec(compile(tree, "<extractor>", "exec"), namespace)  # noqa: S102 - sandboxed by SAFE_BUILTINS+_STDLIB
    except Exception as exc:  # noqa: BLE001 - any exec failure is a rejection
        return f"Rejected: extractor source failed to exec in the sandbox ({type(exc).__name__}: {exc})."
    extract = namespace.get("extract")
    if not callable(extract):
        return "Rejected: extractor source must define a callable named `extract`."
    code_obj = getattr(extract, "__code__", None)
    if (
        code_obj is None
        or code_obj.co_argcount != 2
        or code_obj.co_flags & 0x04  # CO_VARARGS
        or code_obj.co_flags & 0x08  # CO_VARKEYWORDS
    ):
        argcount = code_obj.co_argcount if code_obj is not None else -1
        return f"Rejected: `extract` must take exactly two arguments (html, ctx), got {argcount}."
    return None


def author_html_extractor(ctx: RunContext[AdminAgentDeps], task: str) -> str:
    """Run the nested extractor subagent on ``task`` (a precise description of the
    values to extract + the target template/properties). Returns the validated
    `def extract(html, ctx)` source wrapped in a fenced block, plus its coverage
    stats. The generation agent embeds the source VERBATIM in the bulk script."""
    deps: AdminAgentDeps = ctx.deps
    extractor_agent = deps.extractor_agent
    if extractor_agent is None:
        return "Error: extractor agent is not wired (extractor_agent is None); cannot author an extractor."
    try:
        run = extractor_agent.run_sync(task, deps=deps, usage_limits=UsageLimits(request_limit=MAX_EXTRACTOR_LLM_CALLS))
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
            run_validation_script,
        ],
    )
