"""Import tasks from the CSV the runs table exports (pure).

The export is a snapshot of the prompts (see :mod:`task_export`); importing it
on another instance, or after the runs were deleted, recreates the TASKS from
those prompts. Counters and status are intentionally ignored: they describe a
past execution, not the task. The operator's prompts are the portable part.

Pure: no I/O, no NiceGUI. The web driver reads the uploaded bytes, calls
:func:`parse_tasks_csv`, then hands each prompt to the normal generation path.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass

from .run_name import free_copy_name, validate_run_name

# The two columns an import cannot work without. Everything else in the export
# (status, counters, dates, error) is ignored on purpose.
REQUIRED_COLUMNS: tuple[str, ...] = ("run_id", "prompt")


@dataclass(frozen=True)
class ImportedTask:
    """One task recovered from a CSV row (its exported id + its prompts)."""

    run_id: str
    prompt: str
    validated_prompt: str | None = None


@dataclass(frozen=True)
class TaskImport:
    """The outcome of parsing an uploaded file: tasks, skipped rows, or an error."""

    tasks: tuple[ImportedTask, ...] = ()
    skipped: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        """True when the file was understood well enough to import something."""
        return not self.error


def parse_tasks_csv(text: str) -> TaskImport:
    """Parse exported task CSV text into importable tasks (pure).

    The header must carry ``run_id`` and ``prompt`` (the export's first two
    columns). A row without a prompt, with an unsafe run id, or repeating an id
    already seen is SKIPPED with a human-readable reason instead of aborting the
    whole file: an operator fixing a spreadsheet should not lose the good rows.
    A byte-order mark (Excel) is tolerated.
    """
    content = (text or "").lstrip("\ufeff")
    if not content.strip():
        return TaskImport(error="The file is empty")

    reader = csv.DictReader(io.StringIO(content))
    fields = {(name or "").strip() for name in (reader.fieldnames or ())}
    missing = [column for column in REQUIRED_COLUMNS if column not in fields]
    if missing:
        listed = ", ".join(repr(column) for column in missing)
        return TaskImport(error=f"The CSV is missing the column(s) {listed}. Export the tasks first and import that file.")

    tasks: list[ImportedTask] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(reader, start=2):
        values = {(key or "").strip(): value for key, value in row.items()}
        prompt = _cell(values.get("prompt"))
        if not prompt:
            skipped.append(f"line {line}: no prompt")
            continue
        try:
            run_id = validate_run_name(_cell(values.get("run_id")))
        except ValueError as exc:
            skipped.append(f"line {line}: {exc}")
            continue
        if run_id in seen:
            skipped.append(f"line {line}: duplicate run id {run_id!r}")
            continue
        seen.add(run_id)
        validated = _cell(values.get("validated_prompt")) or None
        # An identical copy is noise: generation falls back to the prompt anyway.
        tasks.append(ImportedTask(run_id=run_id, prompt=prompt, validated_prompt=None if validated == prompt else validated))

    if not tasks:
        reason = "; ".join(skipped) if skipped else "the file has no rows"
        return TaskImport(skipped=tuple(skipped), error=f"No task could be imported ({reason})")
    return TaskImport(tasks=tuple(tasks), skipped=tuple(skipped))


def assign_names(
    tasks: Iterable[ImportedTask],
    taken: Iterable[str],
) -> tuple[tuple[str, ImportedTask], ...]:
    """Pair each task with a run name that does not collide (pure).

    ``taken`` is the instance's existing run ids (plus any in-flight claims). A
    colliding id becomes ``"<id> copy"``/``"<id> copy 2"`` and the batch itself
    is checked too, so one file cannot produce two runs with the same folder.
    """
    used = {str(name).strip() for name in taken if str(name).strip()}
    planned: list[tuple[str, ImportedTask]] = []
    for task in tasks:
        name = task.run_id
        if name in used:
            name = free_copy_name(task.run_id, used)
        used.add(name)
        planned.append((name, task))
    return tuple(planned)


def _cell(value: object) -> str:
    """One CSV cell as stripped text (missing/None -> empty)."""
    return str(value).strip() if value is not None else ""


__all__ = ["REQUIRED_COLUMNS", "ImportedTask", "TaskImport", "assign_names", "parse_tasks_csv"]
