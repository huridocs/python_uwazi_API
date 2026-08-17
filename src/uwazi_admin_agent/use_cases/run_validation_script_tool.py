"""The ``run_validation_script`` tool bound to the script-generation agent (§2.7).

The agent calls this to trial a candidate script against **throwaway dummy
entities in the real instance** before emitting the final :class:`GeneratedScript`.
It supplies the script *and* a ``dummy_spec`` describing the dummies to create
(entities matching the script's target template/shape, so the script's
transformation logic applies meaningfully).

The harness (``DummyEntityHarness``): creates the dummies, runs the script inside
the dummy-scoped exec namespace (the script can only see/touch the dummies),
reverts each original dummy to its exact before raw, checks that revert restored
the exact original state, and **always** deletes the dummies — on success and on
failure. The tool returns a pass/fail report (per-dummy before/after diff +
restore-equality) so the agent can repair the script and re-validate, or emit.

A hard counter on :class:`AdminAgentDeps` caps validation runs per turn
(``MAX_VALIDATION_ATTEMPTS``). When the cap is reached the tool refuses and tells
the agent to emit its best script — the prose limit in the system prompt is not
trusted (LLMs routinely ignore it), so this is the backstop.
"""

from __future__ import annotations

from loguru import logger
from pydantic_ai import RunContext

from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate


async def run_validation_script(
    ctx: RunContext[AdminAgentDeps],
    python_code: str,
    dummy_spec: list[AgentEntityCreate],
) -> str:
    """Validate a candidate script against dummy entities in the real instance.

    Provide ``dummy_spec``: a list of throwaway entities to create, matching the
    template/shape your script targets (representative titles + metadata). The
    harness creates them, runs ``python_code`` against them (your script can only
    see/touch these dummies), reverts them to their exact original state, and
    checks exact restore. Dummies are always deleted afterwards.

    PASS = the script ran without raising AND every dummy's post-revert raw
    equals its original raw (the script's changes are exactly reversible).
    The report also includes the per-dummy before/after diff and your script's
    ``result`` string so you can judge *semantic* correctness and repair.

    You have a HARD limit of ``validation_limit`` attempts per turn. When the
    limit is reached the tool refuses and you must emit the final
    :class:`GeneratedScript` from the exploration you already did.
    """
    deps = ctx.deps
    if deps.validation_attempts >= deps.validation_limit:
        return _limit_reached(deps)

    if deps.entity_api is None:
        return "Error: cannot validate — `entity_api` is missing on dependencies."
    if deps.entity_repository is None:
        return "Error: cannot validate — `entity_repository` is missing on dependencies."

    if not dummy_spec:
        return (
            "# VALIDATION REJECTED — `dummy_spec` is empty.\n"
            "Provide at least one throwaway entity (template_name + title) matching your\n"
            "script's target shape so the harness has dummies to run against. This did NOT\n"
            f"consume a validation attempt. ({deps.validation_attempts}/{deps.validation_limit} used)"
        )

    deps.validation_attempts += 1
    run_number = deps.validation_attempts

    # Local import keeps the stub-free tool module decoupled from the harness at import time.
    from uwazi_admin_agent.configuration import DUMMY_LANGUAGE
    from uwazi_admin_agent.use_cases.dummy_entity_harness import DummyEntityHarness

    harness = DummyEntityHarness(
        entity_api=deps.entity_api,
        relationship_api=deps.relationship_api,
        entity_repository=deps.entity_repository,
        language=DUMMY_LANGUAGE,
    )
    result = await harness.run(python_code, dummy_spec)
    _log_result(result, run_number)
    return _format_result(result, run_number, deps.validation_limit)


def _limit_reached(deps: AdminAgentDeps) -> str:
    return (
        f"# Validation limit reached ({deps.validation_limit}/{deps.validation_limit}).\n"
        "You have used all your validation attempts. STOP calling this tool.\n"
        "Emit the final GeneratedScript now using the patterns you verified during\n"
        "exploration. Do not call run_validation_script again."
    )


def _format_result(result: object, attempt: int, limit: int) -> str:
    """Render a :class:`ValidationResult` as an LLM-readable report."""
    from uwazi_admin_agent.domain.validation_result import ValidationResult

    assert isinstance(result, ValidationResult)
    status = "PASSED" if result.passed else "FAILED"
    remaining = limit - attempt
    header = f"# Validation attempt {attempt}/{limit}: {status}"

    parts: list[str] = [header]

    if result.script_error:
        parts.append(f"## Script error\n{result.script_error}")
    else:
        parts.append(f"## Script result\n{result.script_result if result.script_result is not None else '<result not set>'}")

    diff_lines: list[str] = []
    for d in result.diffs:
        if d.changed:
            kind = "created" if d.before is None else ("deleted" if d.after is None else "modified")
            diff_lines.append(f"  - {d.shared_id}: {kind}")
    parts.append("## Per-dummy diff (changed only)\n" + ("\n".join(diff_lines) if diff_lines else "  (no changes)"))

    if result.restore_equal:
        parts.append("## Restore check\n  OK — every original dummy restored to its exact original raw state.")
    else:
        parts.append("## Restore check\n  MISMATCH — revert did NOT restore exact original state:")
        for m in result.restore_mismatches:
            parts.append(f"  - {m.shared_id}: expected {m.expected!r}\n    actual   {m.actual!r}")

    if result.cleanup_error:
        parts.append(f"## Cleanup\n  WARNING: {result.cleanup_error}")

    if result.passed:
        footer = "\n# VALIDATION PASSED — proceed to emit the final GeneratedScript. Do NOT call this tool again."
    elif remaining > 0:
        footer = (
            f"\n# You have {remaining} validation attempt(s) remaining. "
            "Fix the script and re-run, or emit if you are confident."
        )
    else:
        footer = "\n# This was your LAST validation attempt. Emit the final script now."
    return "\n\n".join(parts) + footer


def _log_result(result: object, run_number: int) -> None:
    from uwazi_admin_agent.domain.validation_result import ValidationResult

    assert isinstance(result, ValidationResult)
    if result.passed:
        changed = sum(1 for d in result.diffs if d.changed)
        logger.info("validation run {} PASSED — restore_equal, {} diff(s)", run_number, changed)
    else:
        logger.warning(
            "validation run {} FAILED — restore_equal={} script_error={} cleanup_error={}",
            run_number,
            result.restore_equal,
            bool(result.script_error),
            bool(result.cleanup_error),
        )
