"""Isolated unit tests for the benchmark tool's pure seams (per AGENTS.md).

No mocks, no ports, no LLM, no Uwazi: ``build_context``, ``call_candidate`` and the
extractor sandbox loader are pure functions over literal inputs. The async
sampling path needs a live instance, so it is exercised by the E2E suite instead.

The invariant these pin: a candidate extractor's failure is DATA (an error string
for that sample), never a crashed benchmark, and a non-``dict`` return is a
contract violation rather than a silent match.
"""

from __future__ import annotations

from uwazi_admin_agent.use_cases.extraction_benchmark_tool import build_context, call_candidate
from uwazi_admin_agent.use_cases.extractor_sandbox import (
    build_extractor_namespace,
    validate_extractor_source,
)
from uwazi_agent.domain.agent_entity import AgentEntity


def _entity() -> AgentEntity:
    return AgentEntity(
        shared_id="abc123",
        title="Case A",
        template_name="Judgment",
        metadata={"case_number": ["2023/111"]},
    )


def test_build_context_matches_the_script_ctx_contract() -> None:
    """A generated script passes exactly {shared_id, title, metadata}; the benchmark
    must hand candidates the SAME dict or a proven extractor behaves differently."""
    context = build_context(_entity())

    assert context == {"shared_id": "abc123", "title": "Case A", "metadata": {"case_number": ["2023/111"]}}


def test_a_missing_title_becomes_an_empty_string_not_none() -> None:
    context = build_context(AgentEntity(shared_id="x", template_name="T"))

    assert context["title"] == ""


def test_call_candidate_returns_the_dict_a_candidate_produced() -> None:
    extractor = build_extractor_namespace('def extract(text, ctx):\n    return {"case_number": "1"}\n')["extract"]

    predicted, error = call_candidate(extractor, "any text", {})

    assert predicted == {"case_number": "1"}
    assert error is None


def test_call_candidate_treats_none_as_no_value_not_an_error() -> None:
    extractor = build_extractor_namespace("def extract(text, ctx):\n    return None\n")["extract"]

    assert call_candidate(extractor, "t", {}) == (None, None)


def test_call_candidate_reports_an_exception_as_data() -> None:
    extractor = build_extractor_namespace("def extract(text, ctx):\n    raise ValueError('boom')\n")["extract"]

    predicted, error = call_candidate(extractor, "t", {})

    assert predicted is None
    assert error is not None and "ValueError" in error and "boom" in error


def test_call_candidate_rejects_a_non_dict_return() -> None:
    """A bare string would otherwise be scored as if it were a field mapping."""
    extractor = build_extractor_namespace("def extract(text, ctx):\n    return '2023/111'\n")["extract"]

    predicted, error = call_candidate(extractor, "t", {})

    assert predicted is None
    assert error is not None and "expected dict" in error


def test_candidate_can_use_the_bound_helpers() -> None:
    """The benchmark sandbox binds the same names the script namespace does, so a
    candidate proven here is loadable in the emitted script."""
    source = (
        "def extract(text, ctx):\n"
        "    hit = dateparse.parse(text)\n"
        "    label = textmatch.best('case number', ['Case No.', 'Claim'], score_cutoff=80)\n"
        "    return {'case_number': hit} if hit else None\n"
    )

    assert validate_extractor_source(source) is None
    predicted, error = call_candidate(build_extractor_namespace(source)["extract"], "Case No. 2023/111", {})
    assert (predicted, error) == (None, None)


def test_an_importing_candidate_is_rejected_before_it_runs() -> None:
    assert validate_extractor_source("import re\ndef extract(a, b):\n    return None\n") is not None


def test_truncation_is_shared_with_the_peek_tools() -> None:
    """The benchmark must feed candidates the SAME window a generated script sees;
    a second hand-rolled truncation silently diverged (different tail size)."""
    from uwazi_admin_agent.use_cases.extraction_benchmark_tool import _truncate_to_max
    from uwazi_admin_agent.use_cases.peek_file_tools import _truncate_peek

    text = "H" * 500 + "TAIL"

    assert _truncate_to_max(text, 100).endswith("TAIL[truncated]")
    # One helper, two limits: no second truncation implementation to drift.
    assert _truncate_to_max(text, 10_000) == text == _truncate_peek(text)
