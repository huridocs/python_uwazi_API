"""Unit tests for the mismatch-driven refinement loop (per AGENTS.md).

Pure functions over literal inputs: mismatch-subset selection, guidance gating,
prompt contract, and the per-turn ledger reset exercised end-to-end through
pydantic-ai's ``TestModel`` (the same offline pattern
``test_pydantic_ai_runtime.py`` / the runtime docs allow). No mocks/stubs of the
code under test, no network, no Uwazi.

The invariants these pin:
- the mismatch EVIDENCE is a bounded subset (variety first: one per property),
  never the full list;
- refinement is offered only when there are mismatches, enough judgeable
  samples, and budget left; a refused offer is stated as a reason, not silence;
- the generalization rule is part of every offered refinement (anti-overfitting
  is a contract, not advice);
- the ledger counts per turn and ``execute()`` resets it at the start of every
  turn.
"""

from __future__ import annotations

import asyncio

from pydantic_ai.models.test import TestModel

from uwazi_admin_agent.configuration import (
    LLM_MAX_OUTPUT_TOKENS,
    MAX_LLM_CALLS,
    MAX_REFINEMENT_MISMATCHES,
    MAX_REFINEMENTS_PER_TURN,
    MIN_SAMPLES_FOR_REFINEMENT,
)
from uwazi_admin_agent.domain.extraction_eval import (
    ExtractionScore,
    FieldMismatch,
    FieldScore,
    MatchPolicy,
    SampleResult,
    score_extraction,
)
from uwazi_admin_agent.use_cases.author_extractor_tool import _EXTRACTOR_SYSTEM_PROMPT
from uwazi_admin_agent.use_cases.extraction_benchmark_tool import (
    pick_refinement_mismatches,
    refinement_guidance,
)
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase
from uwazi_admin_agent.use_cases.system_prompt import SYSTEM_PROMPT


# --- fixtures -----------------------------------------------------------------


def _mismatch(shared_id: str, prop: str, expected: str, predicted: str | None) -> FieldMismatch:
    return FieldMismatch(shared_id=shared_id, property_name=prop, expected=expected, predicted=predicted)


def _score(mismatches: list[FieldMismatch], props: list[str], missing: int = 0) -> ExtractionScore:
    """A score whose FIELD COUNTS match its mismatch list (``mismatched`` and
    ``missing`` are DERIVED properties over ``fields`` — never init kwargs; a
    subset selection therefore reads the per-field counts directly)."""
    counts: dict[str, int] = {}
    for item in mismatches:
        counts[item.property_name] = counts.get(item.property_name, 0) + 1
    fields = {name: FieldScore(property_name=name, mismatched=counts.get(name, 0), missing=missing) for name in props}
    return ExtractionScore(
        policy=MatchPolicy.NORMALIZED,
        fields=fields,
        mismatches=mismatches,
        samples_total=10,
        samples_scored=10,
    )


def _sample(shared_id: str, expected: dict, predicted: dict | None) -> SampleResult:
    return SampleResult(shared_id=shared_id, expected=expected, predicted=predicted)


# --- subset selection ---------------------------------------------------------


def test_subset_is_empty_without_mismatches() -> None:
    score = _score([], ["case_number"])

    assert pick_refinement_mismatches(score) == []


def test_subset_is_capped_at_the_configured_limit() -> None:
    items = [_mismatch(f"id{i}", "case_number", f"e{i}", f"p{i}") for i in range(20)]
    score = _score(items, ["case_number"])

    chosen = pick_refinement_mismatches(score)

    assert len(chosen) == MAX_REFINEMENT_MISMATCHES
    assert all(item in items for item in chosen)


def test_subset_takes_one_mismatch_per_property_first() -> None:
    items = [
        _mismatch("a", "case_number", "e1", "p1"),
        _mismatch("b", "case_number", "e2", "p2"),
        _mismatch("c", "decision_date", "e3", "p3"),
    ]
    score = _score(items, ["case_number", "decision_date"])

    chosen = pick_refinement_mismatches(score)

    props = {item.property_name for item in chosen[:2]}
    assert props == {"case_number", "decision_date"}


def test_subset_prefers_structurally_long_examples_in_remaining_slots() -> None:
    props = ["a", "b"]
    short_a = _mismatch("s1", "a", "x", "y")  # pass-1 pick for property `a`
    long_a = _mismatch("s2", "a", "VERY LONG EXPECTED VALUE" * 5, "some predicted text")
    short_b = _mismatch("s3", "b", "n", "m")  # pass-1 pick for property `b`
    score = _score([short_a, long_a, short_b], props)

    chosen = pick_refinement_mismatches(score)  # limit 5 -> pass 2 reorders per property

    assert chosen[0] is short_a
    assert chosen[1] is short_b
    assert sorted(chosen[2:], key=lambda m: m.shared_id) == sorted([long_a], key=lambda m: m.shared_id)


# --- guidance gating ----------------------------------------------------------


def test_guidance_announces_readiness_when_nothing_mismatched() -> None:
    score = _score([], ["case_number"])

    text = refinement_guidance(score, 0, 10)

    assert "READY" in text


def test_guidance_offers_a_refinement_with_capped_subset_and_generalization_rule() -> None:
    # Same-length texts so the selection order is deterministic: pass 1 takes
    # the property's first mismatch, pass 2 is a stable no-op here.
    items = [_mismatch(f"id{i}", "case_number", f"val{i:03d}", f"out{i:03d}") for i in range(12)]
    score = _score(items, ["case_number"])

    text = refinement_guidance(score, 0, samples_scored=MIN_SAMPLES_FOR_REFINEMENT + 5)

    assert f"RE-AUTHOR the extractor (refinement 1/{MAX_REFINEMENTS_PER_TURN})" in text
    # subset, not the full list: exactly MAX_REFINEMENT_MISMATCHES examples listed
    listed = [line for line in text.splitlines() if line.startswith("  - ")]

    assert len(listed) == MAX_REFINEMENT_MISMATCHES
    assert not any("id11" in line for line in listed)  # only 5 of 12 listed
    assert "GENERALIZE" in text
    assert "do NOT add per-entity special cases" in text
    assert "overfit" in text


def test_guidance_refuses_when_budget_is_spent_and_states_it_as_final() -> None:
    score = _score([_mismatch("a", "case_number", "e", "p")], ["case_number"])

    text = refinement_guidance(score, MAX_REFINEMENTS_PER_TURN, samples_scored=10)

    assert "NOT offered" in text
    assert f"{MAX_REFINEMENTS_PER_TURN} refinements already used" in text
    assert "Treat this score as FINAL" in text
    # no new authoring instruction is issued past the budget
    assert "RE-AUTHOR" not in text


def test_guidance_refuses_when_judgeable_samples_are_too_few() -> None:
    score = _score([_mismatch("a", "case_number", "e", "p")], ["case_number"])

    text = refinement_guidance(score, 0, samples_scored=MIN_SAMPLES_FOR_REFINEMENT - 1)

    assert "NOT offered" in text
    assert f"below the {MIN_SAMPLES_FOR_REFINEMENT}" in text
    assert "RE-AUTHOR" not in text


def test_guidance_is_absent_when_only_missing_but_no_mismatches() -> None:
    """Missing values leave entities untouched: never worth a refinement step."""
    score = _score([], ["case_number"], missing=10)

    assert refinement_guidance(score, 0, 10) == ""


# --- score-shape integration (pure scoring -> guidance) ------------------------


def test_score_extraction_shape_feeds_refinement_guidance() -> None:
    samples = [
        # matched
        _sample("s1", {"case_number": ["2023/111"]}, {"case_number": "2023/111"}),
        # mismatched: the case the refinement loop exists for — a WRONG value
        _sample("s2", {"case_number": ["2023/222"]}, {"case_number": "2023/999"}),
        _sample("s3", {"case_number": ["2023/333"]}, {"case_number": "2023/999"}),
        _sample("s4", {"case_number": ["2023/444"]}, {"case_number": "2023/999"}),
        _sample("s5", {"case_number": ["2023/555"]}, {"case_number": "2023/999"}),
        _sample("s6", {"case_number": ["2023/666"]}, {"case_number": "2023/999"}),
    ]
    score = score_extraction(samples, ["case_number"])

    assert score.mismatched == 5
    assert score.samples_scored == 6
    subset = pick_refinement_mismatches(score)

    assert len(subset) == MAX_REFINEMENT_MISMATCHES
    assert all(item.shared_id in {"s2", "s3", "s4", "s5", "s6"} for item in subset)
    guidance = refinement_guidance(score, 0, samples_scored=score.samples_scored)

    assert "RE-AUTHOR" in guidance
    # each listed mismatch shows the WRONG value it would have written
    assert "2023/999" in guidance


# --- prompt contract -----------------------------------------------------------


def test_generation_prompt_teaches_the_refinement_loop_and_its_cap() -> None:
    for token in (
        "REFINEMENT",
        "NEVER add per-entity special cases",
        "samples are a probe",
        "most 3 refinements per turn",
    ):
        assert token in SYSTEM_PROMPT, token


def test_generation_prompt_gates_refinement_on_the_tools_report() -> None:
    assert "the report says refinement is NOT offered" in SYSTEM_PROMPT


def test_extractor_prompt_teaches_repair_and_the_probe_rule() -> None:
    for token in (
        "REFINEMENT section",
        "special-casing the",
        "listed shared_ids",
        "those samples are a probe",
        "at most 3",
        "refinement is NOT offered",
    ):
        assert token in _EXTRACTOR_SYSTEM_PROMPT, token


# --- ledger reset (real pydantic-ai TestModel run, fully offline) ---------------


class _OfflineThesauriApi:
    pass


def _offline_deps():
    from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
    from uwazi_agent.ports.template_api_port import TemplateApiPort
    from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
    from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort

    class OfflineThesauriApi(ThesauriApiPort):
        async def get_thesauris(self, language):
            return []

        async def get_thesauris_by_names(self, names, language):
            return []

        async def create_thesauri(self, name, values, language, groups=None):
            return {}

        async def update_thesauri(self, name, values, language, groups=None):
            return {}

        async def delete_thesauri(self, name, language):
            return {}

    class OfflineTemplateApi(TemplateApiPort):
        async def get_templates(self):
            return []

        async def get_templates_by_names(self, names):
            return []

        async def get_template_names(self):
            return []

        async def create_template(self, template, language):
            return {}

        async def update_template(self, template, language):
            return {}

        async def delete_template(self, name, language):
            return {}

    class OfflineTemplateMapper(TemplateMapperPort):
        def to_agent(self, api_template):
            return None

        def to_api(self, agent_template):
            return None

    return AdminAgentDeps(
        thesauri_api=OfflineThesauriApi(),
        template_api=OfflineTemplateApi(),
        template_mapper=OfflineTemplateMapper(),
    )


def _run_one_turn(deps) -> None:
    """Run execute() once against TestModel; pydantic-ai emits a canned script."""

    class _Llm:
        def get_model(self):
            return TestModel()

    use_case = GenerateScriptUseCase.__new__(GenerateScriptUseCase)
    use_case._llm = _Llm()  # type: ignore[assignment] - LlmPort seam, offline stand-in per TestModel pattern
    use_case._max_requests = 5
    use_case._max_output_tokens = 512
    use_case._budget = None
    use_case._deps = deps

    async def system_prompt(self) -> str:
        return "prompt"

    use_case._system_prompt = system_prompt.__get__(use_case)  # type: ignore[method-assign]

    asyncio.run(use_case.execute("p"))


def test_execute_resets_the_refinement_ledger_before_the_first_turn() -> None:
    deps = _offline_deps()
    deps.benchmark_refinements_used = 2

    _run_one_turn(deps)

    assert deps.benchmark_refinements_used == 0


def test_execute_resets_the_refinement_ledger_for_every_turn() -> None:
    """A stale non-zero count from an earlier turn must not shorten the next one."""
    deps = _offline_deps()

    _run_one_turn(deps)
    deps.benchmark_refinements_used = MAX_REFINEMENTS_PER_TURN  # exhausted earlier turn
    _run_one_turn(deps)

    assert deps.benchmark_refinements_used == 0


def test_generation_defaults_are_untouched() -> None:
    """The reset feature must not have nudged the generation budget."""
    assert MAX_LLM_CALLS == 70
    assert LLM_MAX_OUTPUT_TOKENS > 0
