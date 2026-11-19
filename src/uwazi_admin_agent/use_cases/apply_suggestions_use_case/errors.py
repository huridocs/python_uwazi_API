"""Apply-path errors (shared by the payload builders and the use case)."""

from __future__ import annotations


def format_write_failures(failures: list[dict[str, str]], limit: int = 3) -> str:
    """One short line per refused write, capped, as the operator-facing message (pure)."""
    if not failures:
        return "no write failures"
    lines = [f"{failure['shared_id'] or '<unknown entity>'}: {failure['error']}" for failure in failures[:limit]]
    remaining = len(failures) - limit
    if remaining > 0:
        lines.append(f"(+{remaining} more)")
    return f"{len(failures)} write(s) refused by Uwazi — " + "; ".join(lines)


class ApplyRefusedError(RuntimeError):
    """The Run was refused (nothing pushable among the selected rows)."""


class ApplyWriteError(RuntimeError):
    """Uwazi refused at least one write of the Run.

    Distinct from :class:`ApplyRefusedError`, which means nothing was even
    attempted: here the Run DID execute and every write that could land landed
    (revertable, like any other write), but the CRUD helpers report a refused
    upload as ``success=False`` in their return value instead of raising. Reading
    those results is what keeps a Uwazi 422 from being reported to the operator
    as a completed Run, so the failures are raised here instead.
    """

    def __init__(self, failures: list[dict[str, str]]) -> None:
        self.failures: list[dict[str, str]] = failures
        super().__init__(format_write_failures(failures))
