"""Isolated unit tests for the refresh outcome (per AGENTS.md).

Pure: a literal count and truncated-bucket list in, a message out. No instance,
network, or stand-ins.
"""

from uwazi_property_filler.domain.refresh_result import RefreshResult


def test_a_complete_refresh_reports_only_the_count() -> None:
    result = RefreshResult(count=23_954)

    assert result.is_complete is True
    assert result.summary() == "Refreshed: 23954 documents"


def test_a_truncated_refresh_says_so_instead_of_implying_completeness() -> None:
    """The list is shorter than the corpus; the operator must be told why."""
    result = RefreshResult(count=9_548, truncated_buckets=[None])

    assert result.is_complete is False
    summary = result.summary()
    assert "Refreshed: 9548 documents" in summary
    assert "all documents" in summary
    assert "Narrow the filter" in summary


def test_a_named_bucket_is_quoted_in_the_summary() -> None:
    result = RefreshResult(count=10_000, truncated_buckets=["Resolutions"])

    assert '"Resolutions"' in result.summary()
