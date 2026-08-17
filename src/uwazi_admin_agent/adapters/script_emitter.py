"""Write a :class:`GeneratedScript` to the run folder as an on-disk artifact.

The admin agent's model is **one script per run** (the generated migration
script), so the emitter writes a single fixed filename — ``script.py`` —
directly under the run folder. This is the "stable name" the Phase 1 DoD calls
for: the same path every time for a given run, overwriting any previous
emission. (``browser_agent``'s date/slug path + lint + sidecar JSON are not
needed here; the manifest already captures ``script`` and ``prompt``.)
"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from uwazi_admin_agent.domain.generated_script import GeneratedScript

SCRIPT_FILENAME = "script.py"


def emit_generated_script(script: GeneratedScript, run_path: Path) -> Path:
    """Write ``script.python_code`` to ``<run_path>/script.py`` and return the path.

    The run folder is created if missing. Overwrites any existing script so the
    latest emission is the on-disk artifact later phases execute.
    """
    run_path = Path(run_path)
    run_path.mkdir(parents=True, exist_ok=True)
    script_path = run_path / SCRIPT_FILENAME
    _ = script_path.write_text(script.python_code, encoding="utf-8")
    logger.debug("generated script emitted to {}", script_path)
    return script_path
