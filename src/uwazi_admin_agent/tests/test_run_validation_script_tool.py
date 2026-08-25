"""Isolated unit tests for the ``run_validation_script`` tool's report formatter.

Per ``AGENTS.md``: no mocks/stubs, no network. ``_format_result`` is a pure
function of a ``ValidationResult`` -\u003e str. These tests build literal
``ValidationResult`` / ``EntityDiff`` values (plain objects, literal data) and
assert the no-op warning is surfaced iff the run PASSED with zero changed diffs.
The gate's ``passed`` semantics are unchanged (``ran_clean AND restore_equal``);
the warning only nudges the LLM, it does not flip the pass.
"""

from uwazi_admin_agent.domain.validation_result import EntityDiff, ValidationResult
from uwazi_admin_agent.use_cases.run_validation_script_tool import _format_result


def _raw(title: str) -> dict:
    return {"_id": "x", "sharedId": "S1", "title": title, "language": "en"}


def test_passed_with_zero_diffs_warns_noop() -> None:
    before = _raw("A")
    result = ValidationResult(
        passed=True,
        script_result="merged 0 entities",
        diffs=[EntityDiff(shared_id="S1", before=before, after=before)],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "# Validation attempt 1/3: PASSED" in report
    assert "## No-op warning" in report
    assert "0 diffs" in report
    assert "query_entities" in report


def test_passed_with_changes_does_not_warn_noop() -> None:
    result = ValidationResult(
        passed=True,
        script_result="merged 2 entities",
        diffs=[
            EntityDiff(shared_id="S1", before=_raw("A"), after=_raw("A-merged")),
            EntityDiff(shared_id="S2", before=_raw("B"), after=None),  # deleted
        ],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" not in report


def test_failed_with_zero_diffs_does_not_warn_noop() -> None:
    """The no-op warning is a PASS-only nudge; a FAILED run already has its own
    diagnostic (script error / restore mismatch), so it must not double-warn."""
    result = ValidationResult(
        passed=False,
        script_error="TypeError: 'AgentEntitySummary' object is not subscriptable",
        diffs=[EntityDiff(shared_id="S1", before=_raw("A"), after=_raw("A"))],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" not in report
    assert "## Script error" in report


def test_passed_noop_warning_still_reports_pass_footer() -> None:
    """The warning must not flip the verdict: the run still PASSED (reversibility
    holds), so the PASS footer is emitted alongside the no-op warning."""
    before = _raw("A")
    result = ValidationResult(
        passed=True,
        script_result="nothing to do",
        diffs=[EntityDiff(shared_id="S1", before=before, after=before)],
        restore_equal=True,
    )

    report = _format_result(result, attempt=1, limit=3)

    assert "## No-op warning" in report
    assert "VALIDATION PASSED" in report
