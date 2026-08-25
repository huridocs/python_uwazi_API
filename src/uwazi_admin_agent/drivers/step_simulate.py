"""``uwazi-admin-agent simulate`` — the standalone re-validation gate (§2.7, Phase 5).

Re-runs :class:`DummyEntityHarness` on the already-emitted ``script.py`` so an
operator can re-prove reversibility before ``execute`` — a deterministic,
non-LLM gate that complements the LLM-driven validation folded into
``generate``. The ``dummy_spec`` is **operator-authored**: a YAML file at
``<run_path>/dummy_spec.yaml`` whose entries validate as
:class:`AgentEntityCreate` (the same shape the LLM supplies to
``run_validation_script``).

PASS = the script ran without raising AND every original dummy's post-revert raw
equals its original raw (§2.7). Prints the harness report; exit 0 on PASS, 1 on
FAIL, 2 on setup error (missing script/spec).

Not unit-tested (needs a real Uwazi instance); validated via the simulation run.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME
from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, RUNS_PATH
from uwazi_admin_agent.domain.validation_result import PLATFORM_MANAGED_FIELDS, ValidationResult
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.dummy_entity_harness import DummyEntityHarness
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate

DUMMY_SPEC_FILENAME = "dummy_spec.yaml"


def run_simulate(run_name: str) -> int:
    """Re-run the dummy-entity validation gate on the emitted script; return an exit code."""
    return asyncio.run(_run_simulate_async(run_name))


async def _run_simulate_async(run_name: str) -> int:
    run_path = RUNS_PATH / run_name
    script_path = run_path / SCRIPT_FILENAME
    spec_path = run_path / DUMMY_SPEC_FILENAME

    if not script_path.is_file():
        print(f"error: no generated script at {script_path} (run `generate` first)", flush=True)
        return 2
    if not spec_path.is_file():
        print(f"error: no dummy spec at {spec_path} (author {DUMMY_SPEC_FILENAME} first)", flush=True)
        return 2

    script = script_path.read_text(encoding="utf-8")
    dummy_spec = _load_dummy_spec(spec_path)

    runtime = build_runtime()
    harness = DummyEntityHarness(
        entity_api=runtime.entity_api,
        relationship_api=runtime.relationship_api,
        entity_repository=runtime.entity_repository,
        language=DUMMY_LANGUAGE,
        search_probe=runtime.search_probe,
    )

    logger.info("simulate: run={} dummies={}", run_name, len(dummy_spec))
    result = await harness.run(script, dummy_spec)

    print(f"simulate: run={run_name} passed={result.passed}")
    if result.script_error:
        print(f"  script error: {result.script_error.splitlines()[0]}")
    elif result.script_result is not None:
        print(f"  script result: {result.script_result}")
    print(f"  restore_equal={result.restore_equal}")
    _print_restore_mismatches(result)
    _print_diffs(result)
    if result.cleanup_error:
        print(f"  cleanup warning: {result.cleanup_error}")
    if result.es_settle_warning:
        print(f"  ES settle warning: {result.es_settle_warning}")
    return 0 if result.passed else 1


def _print_restore_mismatches(result: object) -> None:
    """Print per-dummy restore mismatches: the data keys that differ (excluding
    platform-managed fields) with their expected/actual values."""
    assert isinstance(result, ValidationResult)
    if not result.restore_mismatches:
        return
    print(f"  restore mismatches ({len(result.restore_mismatches)}):")
    for m in result.restore_mismatches:
        differing = _differing_keys(m.expected, m.actual) - PLATFORM_MANAGED_FIELDS
        label = sorted(differing) or "<only platform-managed>"
        print(f"    - {m.shared_id}: differing keys (excl. platform-managed): {label}")
        for key in sorted(differing):
            exp = m.expected.get(key)
            act = m.actual.get(key) if m.actual is not None else None
            print(f"        {key}: expected={exp!r} actual={act!r}")


def _print_diffs(result: object) -> None:
    """Print a one-line per-dummy change summary (created/modified/deleted)."""
    assert isinstance(result, ValidationResult)
    changed = []
    for d in result.diffs:
        if not d.changed:
            continue
        kind = "created" if d.before is None else ("deleted" if d.after is None else "modified")
        changed.append(f"{d.shared_id}:{kind}")
    if changed:
        print(f"  diffs: {', '.join(changed)}")


def _differing_keys(expected: dict[str, Any] | None, actual: dict[str, Any] | None) -> set[str]:
    """Return the keys whose values differ between ``expected`` and ``actual``."""
    if expected is None or actual is None:
        return set()
    return {k for k in set(expected) | set(actual) if expected.get(k) != actual.get(k)}


def _load_dummy_spec(path: Path) -> list[AgentEntityCreate]:
    """Parse a YAML list of ``AgentEntityCreate`` dicts from ``path``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a YAML list of entity specs (got {type(data).__name__})")
    return [AgentEntityCreate.model_validate(item) for item in data]
