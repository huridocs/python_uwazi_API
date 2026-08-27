"""``uwazi-admin-agent dry-run`` — read-only extraction rehearsal against real entities.

Runs the emitted ``script.py`` against real entities with real supporting
files while every write helper is a no-op recorder: per-entity would-be-updates
report + the script's own ``result``, zero mutations. Mirrors
``step_simulate``'s print/exit conventions.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME
from uwazi_admin_agent.configuration import DUMMY_LANGUAGE, RUNS_PATH
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunScriptUseCase

_MAX_PRINTED_RECORDS = 10


def run_dry_run(run_name: str) -> int:
    """Dry-run the emitted script against real entities; return an exit code."""
    return asyncio.run(_run_dry_run_async(run_name))


async def _run_dry_run_async(run_name: str) -> int:
    script_path = RUNS_PATH / run_name / SCRIPT_FILENAME
    if not script_path.is_file():
        print(f"error: no generated script at {script_path} (run `generate` first)", flush=True)
        return 2

    script = script_path.read_text(encoding="utf-8")
    runtime = build_runtime()
    use_case = DryRunScriptUseCase(
        entity_api=runtime.entity_api,
        entity_repository=runtime.entity_repository,
        file_repository=runtime.file_repository,
        default_language=DUMMY_LANGUAGE,
    )

    logger.info("dry-run: run={}", run_name)
    report = await use_case.dry_run(script)

    print(f"dry-run: run={run_name} passed={report.passed}")
    if report.script_error:
        print(f"  script error: {report.script_error.splitlines()[0]}")
    elif report.script_result is not None:
        print(f"  script result: {report.script_result}")
    print(
        f"  would-update={report.would_update} would-create={report.would_create} "
        f"would-delete={report.would_delete} would-publish={report.would_publish} "
        f"would-unpublish={report.would_unpublish} would-rewire={report.would_rewire}"
    )
    for record in report.records[:_MAX_PRINTED_RECORDS]:
        print(f"  - {record}")
    remaining = len(report.records) - _MAX_PRINTED_RECORDS
    if remaining > 0:
        print(f"  ... and {remaining} more")
    return 0 if report.passed else 1
