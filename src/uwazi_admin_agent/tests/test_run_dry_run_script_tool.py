"""Isolated unit tests for the ``run_dry_run_script`` generation-agent tool.

Per ``AGENTS.md``: no mocks/stubs, no network, no env creds. The tool's
limit-gate and report formatting are pure logic over literal
:class:`DryRunReport` values / a literal deps object — the dry-run execution
itself (real namespace composition, real ``run_script_sync``) is covered by
``test_dry_run_namespace.py``; here we pin the agent-facing contract: budget
backstop, unwired-use-case error, report shape, and agent tool registration.
"""

import asyncio

import pytest

from uwazi_admin_agent.configuration import MAX_DRY_RUN_ATTEMPTS
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunReport
from uwazi_admin_agent.use_cases.generate_script_use_case import GenerateScriptUseCase
from uwazi_admin_agent.use_cases.run_dry_run_script_tool import (
    _format_result,
    run_dry_run_script,
)


def _report(**overrides: object) -> DryRunReport:
    base: dict = {
        "passed": True,
        "script_result": "extracted 2 values",
        "script_error": None,
        "would_create": 0,
        "would_update": 2,
        "would_delete": 0,
        "would_publish": 0,
        "would_unpublish": 0,
        "would_rewire": 0,
        "records": [
            {"op": "update", "shared_id": "a", "metadata": {"summary": "2nd"}},
            {"op": "update", "shared_id": "b", "metadata": {"summary": "2nd"}},
        ],
        "samples": [
            {"shared_id": "a", "metadata": {"summary": "2nd"}},
            {"shared_id": "b", "metadata": {"summary": "2nd"}},
        ],
    }
    base.update(overrides)
    return DryRunReport(**base)


class _DepsStub:
    """Minimal stand-in for the pydantic-ai RunContext: only ``deps`` is read."""

    def __init__(self, deps: AdminAgentDeps) -> None:
        self.deps = deps


def _deps(**kwargs: object) -> AdminAgentDeps:
    """Build AdminAgentDeps with its three REQUIRED base fields satisfied.

    The base model requires port instances (arbitrary types allowed); the tool
    never calls these ports in the paths under test (budget gate / unwired use
    case / recorded use case), so a never-called null port is enough.
    """
    from uwazi_agent.ports.template_api_port import TemplateApiPort
    from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
    from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort

    class _NullPort(TemplateApiPort, ThesauriApiPort, TemplateMapperPort):
        """Never called in these tests; satisfies the required base fields."""

        def create_template(self, *a: object, **k: object) -> None: ...

        def delete_template(self, *a: object, **k: object) -> None: ...

        def get_template_names(self, *a: object, **k: object) -> list:
            return []

        def get_templates(self, *a: object, **k: object) -> list:
            return []

        def get_templates_by_names(self, *a: object, **k: object) -> list:
            return []

        def update_template(self, *a: object, **k: object) -> None: ...

        def create_thesauri(self, *a: object, **k: object) -> None: ...

        def delete_thesauri(self, *a: object, **k: object) -> None: ...

        def get_thesauris(self, *a: object, **k: object) -> list:
            return []

        def get_thesauris_by_names(self, *a: object, **k: object) -> list:
            return []

        def update_thesauri(self, *a: object, **k: object) -> None: ...

        def to_agent(self, *a: object, **k: object) -> None:
            return None

        def to_api(self, *a: object, **k: object) -> None:
            return None

    return AdminAgentDeps(thesauri_api=_NullPort(), template_api=_NullPort(), template_mapper=_NullPort(), **kwargs)


# --- agent registration -------------------------------------------------------


def test_generation_agent_registers_dry_run_tool() -> None:
    """The generation agent must expose `run_dry_run_script` so the LLM can
    rehearse against real data before emitting (extraction blind spot)."""
    agent = GenerateScriptUseCase._build_agent("test")
    names = set(agent._function_toolset.tools.keys())
    assert "run_dry_run_script" in names


def test_dry_run_limit_config_is_sane() -> None:
    """A small nonzero budget: enough to fix-and-rerun, small enough to bound
    the turn (same backstop rationale as MAX_VALIDATION_ATTEMPTS)."""
    assert 1 <= MAX_DRY_RUN_ATTEMPTS <= 10


# --- budget gate --------------------------------------------------------------


def test_tool_refuses_when_budget_exhausted() -> None:
    deps = _deps(dry_run_attempts=3, dry_run_limit=3)
    out = asyncio.run(run_dry_run_script(_DepsStub(deps), "result = 1"))
    assert "Dry-run limit reached" in out
    assert deps.dry_run_attempts == 3  # no attempt consumed past the cap


def test_tool_error_when_use_case_unwired() -> None:
    deps = _deps(dry_run_use_case=None)
    out = asyncio.run(run_dry_run_script(_DepsStub(deps), "result = 1"))
    assert "not wired" in out
    assert deps.dry_run_attempts == 0  # a refusal does not consume budget


def test_tool_consumes_budget_and_passes_script_through() -> None:
    calls: list[str] = []

    class RecordingUseCase:
        async def dry_run(self, script: str) -> DryRunReport:
            calls.append(script)
            return _report()

    deps = _deps(dry_run_use_case=RecordingUseCase())
    out = asyncio.run(run_dry_run_script(_DepsStub(deps), "result = 'x'"))
    assert calls == ["result = 'x'"]
    assert deps.dry_run_attempts == 1
    assert "PASSED" in out


# --- report rendering ---------------------------------------------------------


def test_format_result_shows_counters_and_records() -> None:
    out = _format_result(_report(), 1, 3)
    assert "# Dry run attempt 1/3" in out
    assert "(real data, ZERO writes applied): PASSED" in out
    assert "update=2" in out
    assert "- a: {'summary': '2nd'}" in out
    assert "emit the final GeneratedScript" in out


def test_format_result_shows_script_error_and_fail_footer() -> None:
    out = _format_result(_report(passed=False, script_error="NameError: boom", script_result=None), 1, 3)
    assert "FAILED" in out
    assert "NameError: boom" in out
    assert "attempt(s) remaining" in out


def test_format_result_noop_warning_on_zero_writes() -> None:
    out = _format_result(_report(would_update=0, records=[]), 1, 3)
    assert "No-op warning" in out


def test_format_result_samples_more_tail_when_updates_exceed_cap() -> None:
    """`... and N more` compares against would_update, not the sample count."""
    samples = [{"shared_id": f"s{i}", "metadata": {"summary": "x"}} for i in range(3)]
    out = _format_result(_report(samples=samples, would_update=8), 1, 3)
    assert "... and 5 more" in out


def test_report_carries_update_samples() -> None:
    """_format_result renders per-entity update values from `samples`."""
    out = _format_result(_report(), 1, 3)
    assert "First would-be update values" in out
    assert "  - a: {'summary': '2nd'}" in out
    assert "  - b: {'summary': '2nd'}" in out


def test_report_samples_fall_back_to_records_when_empty() -> None:
    """Without samples, the raw records dump is still rendered."""
    out = _format_result(_report(samples=[]), 1, 3)
    assert "First would-be operations" in out
    assert "'summary': '2nd'" in out


def test_format_result_last_attempt_footer() -> None:
    # The PASSED branch takes precedence; use a failed report to see the footer.
    out = _format_result(_report(passed=False, script_error="E"), 3, 3)
    assert "LAST dry-run attempt" in out


# --- RunContext signature compatibility ----------------------------------------


def test_tool_signature_matches_pydantic_ai_expectations() -> None:
    """The tool is registered as a pydantic-ai function tool: first param is a
    RunContext[AdminAgentDeps], the rest are LLM-provided. Pin the signature so
    a refactor can't silently break registration. (The annotation is stored as
    a string under ``from __future__ import annotations``.)"""
    import inspect

    sig = inspect.signature(run_dry_run_script)
    params = list(sig.parameters.values())
    assert "RunContext[AdminAgentDeps]" in str(params[0].annotation)
    assert params[1].name == "python_code"


@pytest.mark.parametrize("field", ["dry_run_use_case", "dry_run_attempts", "dry_run_limit"])
def test_deps_carries_dry_run_fields(field: str) -> None:
    assert field in AdminAgentDeps.model_fields
