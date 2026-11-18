"""The ``benchmark_extractor`` tool: score candidate extractors against KNOWN values.

Choosing an extraction strategy is a measurement, not a guess. Before a
generation turn commits to "label lookup in tables" or "regex over collapsed
text" or "the LLM per entity", it can score each candidate over the SAME handful
of real entities whose target property already holds a value. That known value
is the ground truth; the candidate's output is compared field by field, and the
tool reports accuracy, coverage, and the worst mismatches.

Why this is a TOOL and not part of the generated script:

- the script's job is to produce the review table over the whole target set, and
  it must be deterministic; a benchmark that samples, re-runs several candidates,
  and reports numbers is authoring-time work;
- the numbers must be visible to the LLM BEFORE it writes the script, because
  they decide the script's shape (a strategy at 40 % accuracy must not become a
  bulk write, and a "mismatched" field is the dangerous case: it would write a
  WRONG value where "missing" merely leaves the entity untouched).

Pure scoring lives in :mod:`uwazi_admin_agent.domain.extraction_eval`; this module
only fetches samples (entities, their files/segmentation) and executes the
candidate sources in the same sandbox the emitted script runs in.

LLM-backed candidates are deliberately NOT benchmarked here: such a call spends
the operator's approved budget and is non-deterministic, so it cannot be scored
like a pure function. Compare pure candidates here, and decide about the LLM
separately (a reviewed per-entity `llm_complete`/`llm_map` pass under the caps).
"""

from __future__ import annotations

from typing import Any, Literal

from loguru import logger
from pydantic_ai import RunContext

from uwazi_admin_agent.configuration import MAX_BENCHMARK_CHARS, MAX_BENCHMARK_SAMPLES
from uwazi_admin_agent.domain.extraction_eval import (
    MatchPolicy,
    SampleResult,
    format_score,
    is_blank,
    score_extraction,
)
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.html_extract import is_html_ref
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.extractor_sandbox import build_extractor_namespace, validate_extractor_source
from uwazi_admin_agent.use_cases.peek_file_tools import _truncate_to_max
from uwazi_agent.domain.agent_entity import AgentEntity

SourceKind = Literal["auto", "html", "segmentation"]

# How many extra entities a template scan reads to find enough with a known
# value: the first N entities of a template may all be empty, which would leave
# nothing to judge.
_SCAN_MULTIPLIER: int = 10


def build_context(entity: AgentEntity) -> dict[str, Any]:
    """The ``ctx`` dict a generated script passes to ``extract`` (pure)."""
    return {"shared_id": entity.shared_id, "title": entity.title or "", "metadata": entity.metadata}


def call_candidate(extractor: Any, text: str, context: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Run one candidate over one sample: ``(predicted, error)`` (pure-ish: no I/O).

    A candidate that returns ``None`` produced nothing (missing, not an error);
    anything that is not a dict is a contract violation and is reported as an
    error, so the score cannot silently treat a bare string as a match.
    """
    try:
        output = extractor(text, context)
    except Exception as exc:  # noqa: BLE001 - a candidate failure is data, never a crash
        return None, f"{type(exc).__name__}: {exc}"
    if output is None:
        return None, None
    if not isinstance(output, dict):
        return None, f"extract returned {type(output).__name__}, expected dict | None"
    return output, None


async def _html_text(deps: AdminAgentDeps, shared_id: str, language: str) -> str | None:
    """The entity's first HTML supporting file, decoded and capped (live read)."""
    if deps.entity_repository is None or deps.file_repository is None:
        return None
    raw = await deps.entity_repository.get_raw_by_shared_id(shared_id, language)
    for ref in extract_file_refs(raw):
        entry = ref.model_dump()
        if not is_html_ref(entry):
            continue
        data = await deps.file_repository.get_file_bytes(entry["filename"])
        if data is None:
            continue
        return _truncate_to_max(data.decode("utf-8", errors="replace"), MAX_BENCHMARK_CHARS)
    return None


async def _segmentation_text(deps: AdminAgentDeps, shared_id: str, language: str) -> str | None:
    """The entity's primary-document segmentation as page-ordered text (live read)."""
    if deps.segmentation_repository is None:
        return None
    try:
        segmentation = await deps.segmentation_repository.get_by_shared_id(shared_id, language)
    except Exception as exc:  # noqa: BLE001 - no segmentation is "no source", not a crash
        logger.debug("benchmark: no segmentation for {}: {}", shared_id, exc)
        return None
    ordered = sorted(segmentation.paragraphs, key=lambda p: (p.page_number, p.top, p.left))
    text = "\n".join(paragraph.text for paragraph in ordered if paragraph.text.strip())
    return _truncate_to_max(text, MAX_BENCHMARK_CHARS) if text.strip() else None


async def _sample_text(deps: AdminAgentDeps, shared_id: str, language: str, source: SourceKind) -> str | None:
    """Resolve one entity's extraction input per ``source`` (live reads)."""
    if source == "html":
        return await _html_text(deps, shared_id, language)
    if source == "segmentation":
        return await _segmentation_text(deps, shared_id, language)
    return await _html_text(deps, shared_id, language) or await _segmentation_text(deps, shared_id, language)


def _known_fields(
    entity: AgentEntity,
    property_names: list[str],
    known_values: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """The ground-truth values for one entity: explicit map first, else its metadata (pure)."""
    override = (known_values or {}).get(entity.shared_id) or {}
    known: dict[str, Any] = {}
    for name in property_names:
        if name in override:
            known[name] = override[name]
        else:
            known[name] = entity.metadata.get(name)
    return known


def _has_known_value(known: dict[str, Any]) -> bool:
    """Whether at least one field is judgeable (a blank value cannot score) (pure)."""
    return any(not is_blank(value) for value in known.values())


async def _property_types(
    deps: AdminAgentDeps,
    template_name: str | None,
    entities: list[AgentEntity],
) -> dict[str, str]:
    """``{property: type}`` for the entities' template, so DATE values compare as dates.

    A date property stores epoch seconds, not the ISO string a candidate returns;
    without the type the benchmark would compare two different shapes and report a
    PERFECT date extractor as 0 % (the review path coerces both sides first). An
    unresolvable template yields ``{}`` — the plain rendering rule still applies.
    """
    name = template_name or next((e.template_name for e in entities if e.template_name), None)
    if not name:
        return {}
    try:
        templates = await deps.template_api.get_templates_by_names([name])
    except Exception as exc:  # noqa: BLE001 - types are an accuracy refinement, never fatal
        logger.debug("benchmark: no property types for {}: {}", name, exc)
        return {}
    if not templates:
        return {}
    return {prop.name: str(getattr(prop.type, "value", prop.type)) for prop in templates[0].properties}


async def _load_entities(
    deps: AdminAgentDeps,
    template_name: str | None,
    shared_ids: list[str] | None,
    language: str,
) -> list[AgentEntity]:
    """Resolve the sample entities: explicit ids, else a bounded template scan."""
    if deps.entity_api is None:
        return []
    if shared_ids:
        return await deps.entity_api.get_entities_by_shared_ids(list(shared_ids), language)
    if not template_name:
        return []
    result = await deps.entity_api.get_entities_by_template(
        template_name, language, MAX_BENCHMARK_SAMPLES * _SCAN_MULTIPLIER
    )
    entities = list(result._all_entities) or list(result.examples)
    return entities


async def benchmark_extractor(
    ctx: RunContext[AdminAgentDeps],
    extractors: dict[str, str],
    property_names: list[str],
    template_name: str | None = None,
    shared_ids: list[str] | None = None,
    source: SourceKind = "auto",
    language: str = "en",
    policy: Literal["normalized", "exact", "fuzzy"] = "normalized",
    fuzzy_cutoff: float = 85.0,
    known_values: dict[str, dict[str, Any]] | None = None,
) -> str:
    """Score candidate extractor functions against known values on real samples.

    Use this to DECIDE a strategy with evidence instead of guessing: author two or
    three candidate ``def extract(text, ctx) -> dict | None`` functions (the same
    shape ``author_html_extractor`` returns), then compare them over the same
    entities. The ground truth is the value each entity ALREADY carries in the
    target property (or an explicit ``known_values`` entry); a field the entity
    does not carry is not scored.

    Args:
        extractors: ``{strategy label: source}``, e.g. ``{"table_label": "def extract(...)..."}``.
            Every candidate is validated (no imports, no classes, two arguments) and
            executed in the SAME sandbox the generated script runs in.
        property_names: the template property names the extractors should produce.
        template_name: sample entities of this template (a bounded scan finds ones
            whose properties carry known values).
        shared_ids: explicit sample entities; wins over ``template_name``.
        source: which text each candidate receives — ``html`` (first HTML
            supporting file), ``segmentation`` (primary-document paragraphs), or
            ``auto`` (HTML when present, else segmentation).
        language: entity row locale for reads (ISO 639-1).
        policy: comparison strictness — ``normalized`` (default: case/whitespace/
            punctuation-insensitive), ``exact``, or ``fuzzy`` (``textmatch``
            similarity at ``fuzzy_cutoff``).
        fuzzy_cutoff: similarity threshold for ``policy="fuzzy"`` (0..100).
        known_values: optional ``{shared_id: {property: value}}`` overriding the
            entity metadata as ground truth (use it to benchmark properties that
            are still empty).

    Returns:
        A text report per strategy — accuracy (matched / judged), coverage, and the
        worst mismatches with their expected value — plus a one-line ranking. A
        strategy below the operator's comfort level must not be shipped as a bulk
        write: regenerate with a different strategy, or leave the entities untouched.
    """
    deps: AdminAgentDeps = ctx.deps
    if not extractors:
        return "Error: extractors must contain at least one {label: source} entry."
    if not property_names:
        return "Error: property_names must name at least one template property to score."
    if source not in ("auto", "html", "segmentation"):
        return "Error: source must be 'auto', 'html' or 'segmentation'."
    for label, candidate in extractors.items():
        rejection = validate_extractor_source(candidate)
        if rejection is not None:
            return f"Error: candidate {label!r} was rejected: {rejection}"

    entities = await _load_entities(deps, template_name, shared_ids, language)
    if not entities:
        return (
            "Error: no sample entities resolved. Pass shared_ids, or a template_name that exists "
            "and has entities (see list_templates / query_entities)."
        )

    property_types = await _property_types(deps, template_name, entities)
    candidates = {label: build_extractor_namespace(candidate)["extract"] for label, candidate in extractors.items()}
    # One outcome list per candidate over the SAME samples: the comparison is only
    # meaningful when every strategy saw identical inputs.
    outcomes: dict[str, list[SampleResult]] = {label: [] for label in candidates}
    sampled = 0
    for entity in entities:
        if sampled >= MAX_BENCHMARK_SAMPLES:
            break
        known = _known_fields(entity, property_names, known_values)
        if not _has_known_value(known):
            continue  # nothing to judge on this entity
        text = await _sample_text(deps, entity.shared_id, language, source)
        context = build_context(entity)
        for label, extractor in candidates.items():
            if text is None:
                outcomes[label].append(
                    SampleResult(
                        shared_id=entity.shared_id,
                        expected=known,
                        error=f"no {source} source text available",
                    )
                )
                continue
            value, error = call_candidate(extractor, text, context)
            outcomes[label].append(SampleResult(shared_id=entity.shared_id, expected=known, predicted=value, error=error))
        sampled += 1

    if not sampled:
        return (
            f"NO JUDGEABLE samples: the first entities of {template_name or 'the given ids'} carry no known "
            f"value for {property_names}. Benchmark properties that are already filled, or pass known_values."
        )

    reports: list[tuple[str, Any]] = []
    for label in candidates:
        score = score_extraction(
            outcomes[label],
            property_names,
            policy=MatchPolicy(policy),
            fuzzy_cutoff=fuzzy_cutoff,
            property_types=property_types,
        )
        reports.append((label, score))

    # Best first: highest accuracy, then FEWEST mismatches (a wrong value is the
    # case that writes bad data), then the label so the order is stable.
    ranked = sorted(
        reports,
        key=lambda item: (
            -(item[1].accuracy or 0.0),
            item[1].mismatched,
            item[0],
        ),
    )
    lines = [
        f"benchmark: {sampled} sample(s), source={source}, language={language}",
        f"ground truth: entity metadata for {property_names}" + (" (with known_values overrides)" if known_values else ""),
        "ranking (best first): "
        + ", ".join(
            f"{label} {'n/a' if score.accuracy is None else f'{100 * score.accuracy:.1f}%'}" for label, score in ranked
        ),
        "",
    ]
    for label, score in reports:
        lines.append(format_score(score, strategy=label))
        lines.append("")
    lines.append(
        "Reminder: 'mismatched' values would be WRITTEN by a bulk pass (a wrong value), while "
        "'missing' leaves the entity untouched. A strategy must clear the operator's bar on BOTH "
        "before it becomes a generated script; below it, refine the extractor or leave the task to "
        "a reviewed prompt instead of a wide write."
    )
    return "\n".join(lines)


__all__ = [
    "benchmark_extractor",
    "build_context",
    "call_candidate",
]
