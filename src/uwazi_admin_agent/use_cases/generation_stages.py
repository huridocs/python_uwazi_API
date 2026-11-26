"""Operator-facing stage text for a running generation turn (pure).

The generation agent works through a long sequence of tool calls — schema
reads, entity sampling, extractor authoring (an LLM sub-turn), benchmarks, and
finally the script emit. The tasks table shows only "creating" for the whole
span, which reads as a frozen spinner. This module maps what the agent is
actually doing onto one short sentence so the row can say it.

Pure: no I/O, no NiceGUI, no pydantic-ai import (the caller passes tool names).
"""

from __future__ import annotations

# The stages, in the order a typical turn reaches them. The first is the
# default: the model is thinking before it calls any tool.
STAGE_THINKING = "LLM is working out the plan…"
STAGE_SCRIPT = "LLM is writing the script…"
STAGE_PLANNING = "Running the generated script (planning pass)…"
STAGE_SAVING = "Saving the script and the review table…"
# The creation flow's ONE self-repair round: the freshly generated script
# crashed in its first planning pass, so the crash is fed back to the authoring
# agent before the run is declared failed.
STAGE_REPAIR = "The script crashed while planning — the LLM is repairing it and re-running the pass…"

_TOOL_STAGES: dict[str, str] = {
    # Schema / discovery reads.
    "list_templates": "Reading the instance's templates…",
    "get_templates_by_names": "Reading the target template's schema…",
    "list_thesauri": "Reading the instance's thesauri…",
    "get_thesauris_by_names": "Reading the thesaurus labels…",
    "query_entities": "Sampling the target entities…",
    "query_entities_full": "Sampling the target entities…",
    # Source reads.
    "peek_entity_files": "Reading the entities' files…",
    "peek_file_text": "Reading an entity's file…",
    "peek_segmentation": "Reading a document's segmentation…",
    "peek_segmentation_by_entity": "Reading a document's segmentation…",
    "peek_url_text": "Reading a source page…",
    # Extraction strategy.
    "author_html_extractor": "LLM is authoring the extraction function…",
    "benchmark_extractor": "Running the extraction benchmark…",
}


def tool_stage(tool_name: str) -> str:
    """The stage sentence for one tool call (pure).

    An unknown tool (a future addition) reports the generic "working" stage
    rather than an empty caption, so the row is never silent mid-turn.
    """
    return _TOOL_STAGES.get(tool_name, STAGE_THINKING)


def benchmark_stage(round: int) -> str:
    """The benchmark stage, numbered when the turn re-benchmarks (pure)."""
    return f"Running the extraction benchmark (round {round})…" if round > 1 else _TOOL_STAGES["benchmark_extractor"]


__all__ = [
    "STAGE_PLANNING",
    "STAGE_SAVING",
    "STAGE_SCRIPT",
    "STAGE_THINKING",
    "benchmark_stage",
    "tool_stage",
]
