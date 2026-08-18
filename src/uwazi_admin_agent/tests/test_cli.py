"""Isolated unit tests for the Phase-5 CLI (per AGENTS.md: no mocks, no network).

Covers the behavioral DoD:
- ``uwazi-admin-agent --help`` lists the subcommands.
- ``list-runs`` runs against an empty runs root without error.

The live-wired steps (generate/simulate/execute/revert/verify) need an LLM + a
real Uwazi instance and are not unit-tested here (validated via the simulation
run, matching Phases 2-4). The read-only steps' pure rendering + the backup-store
wiring primitives are tested with literal inputs and a tmp runs root.

Phase 6 adds parser-shape tests for the new ``verify`` subcommand and the
``execute --on-error`` flag.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.drivers.cli import build_parser, main
from uwazi_admin_agent.drivers.runtime import build_backup_store
from uwazi_admin_agent.drivers.step_inspect_run import render_manifest_summary
from uwazi_admin_agent.drivers.step_list_runs import run_list_runs

# --- parser shape -----------------------------------------------------------


def test_build_parser_help_lists_all_subcommands() -> None:
    help_text = build_parser().format_help()

    for subcommand in (
        "generate",
        "simulate",
        "execute",
        "revert",
        "verify",
        "list-runs",
        "inspect-run",
    ):
        assert subcommand in help_text, f"subcommand {subcommand!r} missing from --help"


def test_build_parser_dispatches_list_runs_command() -> None:
    args = build_parser().parse_args(["list-runs"])

    assert args.command == "list-runs"


def test_build_parser_accepts_run_override_for_per_run_steps() -> None:
    for command in ("simulate", "execute", "revert", "verify", "inspect-run"):
        args = build_parser().parse_args([command, "--run", "my-run"])
        assert args.command == command
        assert args.run == "my-run"


def test_build_parser_rejects_unknown_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bogus"])


# --- execute --on-error flag (Phase 6) -------------------------------------


def test_build_parser_execute_defaults_on_error_to_none() -> None:
    # None means the driver falls back to DEFAULT_ON_ERROR_POLICY.
    args = build_parser().parse_args(["execute"])
    assert args.command == "execute"
    assert args.on_error is None


def test_build_parser_execute_accepts_on_error_choices() -> None:
    for value in ("stop", "stop-and-revert"):
        args = build_parser().parse_args(["execute", "--on-error", value])
        assert args.on_error == value


def test_build_parser_execute_rejects_invalid_on_error() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["execute", "--on-error", "nuke"])


def test_build_parser_verify_subcommand_parses() -> None:
    args = build_parser().parse_args(["verify", "--run", "r"])
    assert args.command == "verify"
    assert args.run == "r"


# --- list-runs DoD -----------------------------------------------------------


def test_main_list_runs_returns_zero_and_passthrough_store_contents(capsys) -> None:
    # ``list-runs`` is a faithful passthrough of ``FilesystemBackupStore(RUNS_PATH)
    # .list_runs()`` and exits 0 — whether or not the package runs root has runs
    # (manual runs populate it). The empty-root guarantee is covered by the
    # ``build_backup_store`` tests below; here we assert the driver wiring.
    expected = build_backup_store().list_runs()

    exit_code = main(["list-runs"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.split() == expected


def test_run_list_runs_returns_zero_and_passthrough_store_contents(capsys) -> None:
    expected = build_backup_store().list_runs()

    exit_code = run_list_runs()

    assert exit_code == 0
    assert capsys.readouterr().out.split() == expected


# --- backup-store wiring primitives (tmp runs root, no network) --------------


def test_build_backup_store_lists_manifest_runs_sorted(tmp_path: Path) -> None:
    store = build_backup_store(tmp_path)
    for name in ("zeta", "alpha", "mid"):
        store.save_manifest(name, _minimal_manifest(name))

    assert store.list_runs() == ["alpha", "mid", "zeta"]


def test_build_backup_store_empty_root_lists_nothing(tmp_path: Path) -> None:
    # A root that does not exist yet (fresh tmp_path has no runs/ subdir).
    assert build_backup_store(tmp_path / "runs").list_runs() == []


def test_build_backup_store_ignores_folders_without_manifest(tmp_path: Path) -> None:
    store = build_backup_store(tmp_path)
    store.save_manifest("real", _minimal_manifest("real"))
    (tmp_path / "partial").mkdir()
    (tmp_path / "partial" / "script.py").write_text("pass", encoding="utf-8")

    assert store.list_runs() == ["real"]


# --- inspect-run pure rendering ----------------------------------------------


def test_render_manifest_summary_lists_status_counts_prompt_and_script(tmp_path: Path) -> None:
    manifest = _minimal_manifest("repair-titles")
    script_path = tmp_path / "repair-titles" / "script.py"

    summary = render_manifest_summary(manifest, "repair-titles", script_path)

    assert "run: repair-titles" in summary
    assert "status: planned" in summary
    assert "modified: 0" in summary
    assert "deleted: 0" in summary
    assert "created: 0" in summary
    assert "rewired: 0" in summary
    assert f"script: {script_path}" in summary
    assert "prompt: Set every Court title to uppercase" in summary


def test_render_manifest_summary_reflects_populated_counts(tmp_path: Path) -> None:
    from uwazi_admin_agent.domain.snapshot import EntityIdentity

    manifest = _minimal_manifest("r")
    manifest.modified = [EntityIdentity(shared_id="A1", language="en")]
    manifest.created = [EntityIdentity(shared_id="C1", language="en")]
    manifest.deleted = [EntityIdentity(shared_id="D1", language="en")]
    manifest.rewired = []  # rewired needs RewiredRelationship; leave empty for the count
    manifest.status = RunStatus.EXECUTED

    summary = render_manifest_summary(manifest, "r", tmp_path / "r" / "script.py")

    assert "status: executed" in summary
    assert "modified: 1" in summary
    assert "created: 1" in summary
    assert "deleted: 1" in summary
    assert "rewired: 0" in summary


# --- helpers -----------------------------------------------------------------


def _minimal_manifest(run_id: str) -> MigrationManifest:
    return MigrationManifest(
        run_id=run_id,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        prompt="Set every Court title to uppercase",
        script="pass",
        status=RunStatus.PLANNED,
    )
