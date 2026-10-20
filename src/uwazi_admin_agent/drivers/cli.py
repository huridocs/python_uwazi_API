"""The ``uwazi-admin-agent`` CLI entrypoint (Phase 5).

A single argparse dispatcher with one subcommand per run-lifecycle step
(§2.2): ``generate`` → ``scan`` → ``revert``, plus the read-only
``list-runs`` and ``inspect-run``. Each subcommand delegates to a
thin step driver in ``drivers/step_<name>.py`` that wires adapters to a use
case and runs it — **no business logic lives here** (§4: drivers → use_cases +
adapters).

``text-property-e2e`` is the LIVE end-to-end exercise: it drives the whole UI
flow (create → plan → Run → revert → verify) against the configured instance,
the way the web UI does, and reports what it found.

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
from uwazi_admin_agent.drivers.step_generate import run_generate
from uwazi_admin_agent.drivers.step_inspect_run import run_inspect_run
from uwazi_admin_agent.drivers.step_list_runs import run_list_runs
from uwazi_admin_agent.drivers.step_revert import run_revert
from uwazi_admin_agent.drivers.step_scan import run_scan
from uwazi_admin_agent.drivers.step_text_property_e2e import main as run_text_property_e2e_main
from uwazi_admin_agent.drivers.step_verify import run_verify


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser with one subparser per run-lifecycle step."""
    parser = argparse.ArgumentParser(
        prog="uwazi-admin-agent",
        description="Admin agent: turn a natural-language prompt into a safe, revertable Uwazi migration.",
    )
    sub = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{generate,scan,revert,verify,list-runs,inspect-run,text-property-e2e}",
    )

    sub.add_parser(
        "generate",
        help="Generate the script for the active run.",
    )

    p_scan = sub.add_parser(
        "scan",
        help="Run the script as the planning pass (recorded no-op writes; the review table's source).",
    )
    p_scan.add_argument("--run", default=None, help="Run name (default: the active run in active_run.yaml).")

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

    p_e2e = sub.add_parser(
        "text-property-e2e",
        help=(
            "Exercise the per-language text-property update + revert path against the LIVE instance, "
            "the way the UI does it (three tasks: generate, plan, Run, revert, verify)."
        ),
    )
    p_e2e.add_argument("--user", default=None, help="Uwazi user (default: UWAZI_USER, else admin).")
    p_e2e.add_argument("--password", default=None, help="Uwazi password (default: UWAZI_PASSWORD, else admin).")
    p_e2e.add_argument("--template", default=None, help="Template to exercise (default: DOCUMENT).")
    p_e2e.add_argument("--property", default=None, help="Text property to write (default: the template's first one).")
    p_e2e.add_argument("--entities", type=int, default=None, help="Entities per language (default: 2).")
    p_e2e.add_argument("--keep-runs", action="store_true", help="Keep the runs on disk (default: delete them).")

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
    if command == "scan":
        return run_scan(run_name=_resolve_run_name(args.run))
    if command == "revert":
        return run_revert(run_name=_resolve_run_name(args.run))
    if command == "verify":
        return run_verify(run_name=_resolve_run_name(args.run))
    if command == "list-runs":
        return run_list_runs()
    if command == "inspect-run":
        return run_inspect_run(run_id=_resolve_run_name(args.run))
    if command == "text-property-e2e":
        return run_text_property_e2e_main(
            [
                *(["--user", args.user] if args.user else []),
                *(["--password", args.password] if args.password else []),
                *(["--template", args.template] if args.template else []),
                *(["--property", args.property] if args.property else []),
                *(["--entities", str(args.entities)] if args.entities else []),
                *(["--keep-runs"] if args.keep_runs else []),
            ]
        )

    parser.error(f"unknown command: {command}")  # pragma: no cover — argparse guards this
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
