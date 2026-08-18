"""The ``uwazi-admin-agent`` CLI entrypoint (Phase 5).

A single argparse dispatcher with one subcommand per run-lifecycle step
(§2.2): ``generate`` → ``simulate`` → ``execute`` → ``revert``, plus the
read-only ``list-runs`` and ``inspect-run``. Each subcommand delegates to a
thin step driver in ``drivers/step_<name>.py`` that wires adapters to a use
case and runs it — **no business logic lives here** (§4: drivers → use_cases +
adapters).

The console entry ``uwazi-admin-agent`` (added to ``[project.scripts]`` in
``pyproject.toml``) points at :func:`main`.

Read-only steps (``list-runs``, ``inspect-run``) need only the backup store;
mutating steps build the full :class:`Runtime` (live Uwazi + LLM). ``--run``
overrides the active run name for the per-run steps; by default the active run
is read from ``active_run.yaml``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from uwazi_admin_agent.adapters.runs_config_loader import RunsConfigLoader
from uwazi_admin_agent.drivers.step_execute import run_execute
from uwazi_admin_agent.drivers.step_generate import run_generate
from uwazi_admin_agent.drivers.step_inspect_run import run_inspect_run
from uwazi_admin_agent.drivers.step_list_runs import run_list_runs
from uwazi_admin_agent.drivers.step_revert import run_revert
from uwazi_admin_agent.drivers.step_simulate import run_simulate
from uwazi_admin_agent.drivers.step_verify import run_verify


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with one subparser per run-lifecycle step."""
    parser = argparse.ArgumentParser(
        prog="uwazi-admin-agent",
        description="Admin agent: turn a natural-language prompt into a safe, revertable Uwazi migration.",
    )
    sub = parser.add_subparsers(
        dest="command", required=True, metavar="{generate,simulate,execute,revert,verify,list-runs,inspect-run}"
    )

    sub.add_parser(
        "generate",
        help="Generate the migration script for the active run (includes LLM-driven dummy validation).",
    )

    p_simulate = sub.add_parser(
        "simulate",
        help="Re-run the dummy-entity validation gate on the emitted script using a local dummy_spec.yaml.",
    )
    p_simulate.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")

    p_execute = sub.add_parser(
        "execute",
        help="Execute the validated script against real entities with backup-intercepted CRUD.",
    )
    p_execute.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")
    p_execute.add_argument(
        "--on-error",
        choices=("stop", "stop-and-revert"),
        default=None,
        help="What to do if the script raises mid-run (default: stop).",
    )

    p_revert = sub.add_parser(
        "revert",
        help="Revert a run by restoring backed-up entities and deleting created ones, then verify the restore.",
    )
    p_revert.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")

    p_verify = sub.add_parser(
        "verify",
        help="Verify a reverted run: fetch current raws and confirm they match the snapshots.",
    )
    p_verify.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")

    sub.add_parser("list-runs", help="List run ids known to the backup store (one per line).")

    p_inspect = sub.add_parser("inspect-run", help="Print a run's manifest summary (status, counts, prompt, script path).")
    p_inspect.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")

    return parser


def _resolve_run_name(name: str | None) -> str:
    """Return ``name`` or the active run name from ``active_run.yaml``."""
    if name is not None:
        return name
    return RunsConfigLoader.default().load_active_name()


def main(argv: Sequence[str] | None = None) -> int:
    """Parse ``argv`` and dispatch to the matching step driver; return its exit code."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    command: str = args.command

    if command == "generate":
        return run_generate()
    if command == "simulate":
        return run_simulate(run_name=_resolve_run_name(args.run))
    if command == "execute":
        return run_execute(run_name=_resolve_run_name(args.run), on_error=args.on_error)
    if command == "revert":
        return run_revert(run_name=_resolve_run_name(args.run))
    if command == "verify":
        return run_verify(run_name=_resolve_run_name(args.run))
    if command == "list-runs":
        return run_list_runs()
    if command == "inspect-run":
        return run_inspect_run(run_name=_resolve_run_name(args.run))

    parser.error(f"unknown command: {command}")  # pragma: no cover — argparse guards this
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
