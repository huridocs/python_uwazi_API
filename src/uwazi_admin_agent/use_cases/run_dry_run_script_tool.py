"""The ``run_dry_run_script`` tool bound to the script-generation agent.

The dummy gate (``run_validation_script``) proves a candidate script against
throwaway dummies — but dummies carry **no uploaded files**, so for extraction
tasks the fetch→extract→update path never executes there (``get_file_bytes``
returns ``None``). The dry run closes that blind spot: it runs the candidate
script against **real entities with real supporting files** while every write
helper is a no-op recorder (:class:`DryRunScriptUseCase` — real reads, zero
mutations). The tool returns the per-op would-be-write report + the script's
``result`` so the agent can judge semantic correctness against REAL data
(match rates, per-entity values, chunking) BEFORE emitting the final script.

A hard counter on :class:`AdminAgentDeps` caps dry runs per turn
(``MAX_DRY_RUN_ATTEMPTS``) — same backstop pattern as the dummy gate.
"""

from __future__ import annotations

from loguru import logger
from pydantic_ai import RunContext

from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps


async def run_dry_run_script(ctx: RunContext[AdminAgentDeps], python_code: str) -> str:
    """Rehearse a candidate script against the REAL entities, writing nothing.

    Runs ``python_code`` in the dry-run exec namespace: ``query_entities`` /
    ``get_entity_files`` / ``get_file_bytes`` perform REAL reads against the
    live instance, while all write helpers (``create_entities``,
    ``update_entities``, ``delete_entities``, ``publish_entities``,
    ``unpublish_entities``, ``set_publish_status``, ``create_relationships``,
    ``move_files_to_entity``) only RECORD what they would have written.

    Use this AFTER the dummy gate passed, to prove the script against real data
    (real HTML supporting files, real metadata shapes, real match rates) with
    zero mutations. The report shows every would-be write so you can verify the
    per-entity values and the match rate before emitting the final script.
    """
    deps = ctx.deps
    if deps.dry_run_attempts >= deps.dry_run_limit:
        return (
            f"# Dry-run limit reached ({deps.dry_run_limit}/{deps.dry_run_limit}).\n"
            "You have used all your dry-run attempts. STOP calling this tool.\n"
            "Emit the final GeneratedScript now using what the dry runs showed. "
            "Do not call run_dry_run_script again."
        )
    if deps.dry_run_use_case is None:
        return "Error: cannot dry-run — the dry-run use case is not wired on dependencies."

    deps.dry_run_attempts += 1
    attempt = deps.dry_run_attempts

    logger.info("dry run {} starting", attempt)
    report = await deps.dry_run_use_case.dry_run(python_code)
    _log_result(report, attempt)
    return _format_result(report, attempt, deps.dry_run_limit)


def _format_result(report: object, attempt: int, limit: int) -> str:
    """Render a :class:`DryRunReport` as an LLM-readable report."""
    from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunReport

    assert isinstance(report, DryRunReport)
    status = "PASSED" if report.passed else "FAILED"
    remaining = limit - attempt
    parts: list[str] = [f"# Dry run attempt {attempt}/{limit} (real data, ZERO writes applied): {status}"]

    if report.script_error:
        parts.append(f"## Script error\n{report.script_error}")
    else:
        parts.append(f"## Script result\n{report.script_result if report.script_result is not None else '<result not set>'}")

    parts.append(
        "## Would-be writes\n"
        f"  update={report.would_update} create={report.would_create} delete={report.would_delete} "
        f"publish={report.would_publish} unpublish={report.would_unpublish} rewire={report.would_rewire}"
    )

    if report.samples:
        lines = [f"  - {s['shared_id']}: {s['metadata']}" for s in report.samples]
        parts.append("## First would-be update values (max 20, per entity)\n" + "\n".join(lines))
        total_updates = report.would_update
        if total_updates > len(report.samples):
            parts.append(f"  ... and {total_updates - len(report.samples)} more")
    elif report.records:
        shown = report.records[:20]
        lines = [f"  - {record}" for record in shown]
        parts.append("## First would-be operations (max 20)\n" + "\n".join(lines))
        if len(report.records) > len(shown):
            parts.append(f"  ... and {len(report.records) - len(shown)} more")

    if report.passed:
        if report.would_update == 0 and report.would_create == 0 and report.would_delete == 0:
            parts.append(
                "## No-op warning\n"
                "  The dry run recorded 0 writes. On a prompt that asks for a change this\n"
                "  means the script found nothing to change against the REAL entities —\n"
                "  check your `query_entities` access, the match logic, and the `result`\n"
                "  counts. A no-op script is not a successful extraction."
            )
        footer = (
            "\n# DRY RUN PASSED on real data with zero mutations. If the would-be values "
            "and counts look correct, emit the final GeneratedScript."
        )
    elif remaining > 0:
        footer = f"\n# Fix the script and re-run the dry run ({remaining} attempt(s) remaining), or emit if confident."
    else:
        footer = "\n# This was your LAST dry-run attempt. Emit the final script now."
    return "\n\n".join(parts) + footer


def _log_result(report: object, run_number: int) -> None:
    from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunReport

    assert isinstance(report, DryRunReport)
    if report.passed:
        logger.info(
            "dry run {} PASSED — would_update={} would_create={} would_delete={}",
            run_number,
            report.would_update,
            report.would_create,
            report.would_delete,
        )
    else:
        logger.warning("dry run {} FAILED — script_error={}", run_number, bool(report.script_error))
