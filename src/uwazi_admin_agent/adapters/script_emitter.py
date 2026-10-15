"""Write a :class:`GeneratedScript` to the run folder as an on-disk artifact.

A generation turn emits ONE script under a stable filename directly under the
run folder — the "stable name" the Phase 1 DoD calls for: the same path every
time for a given run, overwriting any previous emission:

- ``script.py`` — THE script: the planning pass (read-only discovery + declared
  changes; it runs dry everywhere and its recorded writes are the review
  table's entity rows).

(``browser_agent``'s date/slug path + lint + sidecar JSON are not needed here;
the manifest already captures ``script`` and ``prompt``.)
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from uwazi_admin_agent.domain.generated_script import GeneratedScript

SCRIPT_FILENAME = "script.py"


def emit_generated_script(script: GeneratedScript, run_path: Path) -> Path:
    """Write ``script.python_code`` (the planning-pass script) to ``<run_path>/script.py``.

    The run folder is created if missing. Overwrites any previous emission so
    the latest script is the on-disk artifact the planning scan runs.
    """
    return _emit(script.python_code, run_path, SCRIPT_FILENAME)


def _emit(code: str, run_path: Path, filename: str) -> Path:
    """One script body to one stable run-folder filename (shared impl)."""
    run_path = Path(run_path)
    run_path.mkdir(parents=True, exist_ok=True)
    script_path = run_path / filename
    _ = script_path.write_text(code, encoding="utf-8")
    logger.debug("{} emitted to {}", filename, script_path)
    return script_path
