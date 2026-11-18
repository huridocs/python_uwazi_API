"""Isolated unit tests for the ``dateparse`` sandbox namespace (per AGENTS.md).

Pure detection over literal strings: no mocks, no network. These pin the safety
property that makes the helper usable in a bulk write — an arbitrary prose token
must NOT become a date, because a fabricated date is written straight into the
entity's metadata.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.date_parse import find, is_date, parse


def test_parses_iso_date() -> None:
    assert parse("2026-11-17") == "2026-11-17"


def test_parses_day_month_name_year_and_month_day_year() -> None:
    assert parse("Judgment of 17 November 2026") == "2026-11-17"
    assert parse("November 17, 2026") == "2026-11-17"


def test_parses_ordinal_day_with_month_abbreviation() -> None:
    assert parse("17th Nov. 2026") == "2026-11-17"


def test_parses_numeric_date_in_both_orders() -> None:
    assert parse("11/17/2026") == "2026-11-17"
    assert parse("17/11/2026") == "2026-11-17"


def test_ambiguous_numeric_date_follows_dayfirst() -> None:
    """05/06/2026 is May 6th in US order and June 5th day-first: the flag decides,
    and an UNAMBIGUOUS token (17/11) ignores it."""
    assert parse("05/06/2026", dayfirst=False) == "2026-05-06"
    assert parse("05/06/2026", dayfirst=True) == "2026-06-05"
    assert parse("17/11/2026", dayfirst=True) == "2026-11-17"


def test_rejects_prose_numbers_that_a_fuzzy_parser_invents_dates_from() -> None:
    """These all parse to a real calendar day under dateutil's fuzzy mode; the
    gate must refuse them so no fabricated date reaches Uwazi."""
    for text in ("section 4.2", "3.5 million", "v1.2.3", "the year 2023 was hard", "12345"):
        assert parse(text) is None, text


def test_rejects_date_shaped_case_numbers() -> None:
    assert parse("Case No. 2023/111") is None
    assert parse("No. 17/2026") is None


def test_rejects_a_date_without_an_explicit_year() -> None:
    """'17 Nov' and 'November 2026' are partial: the day/month is a guess from the
    current date, which a bulk write must never store."""
    assert parse("17 Nov") is None
    assert parse("November 2026") is None
    assert parse("2026") is None


def test_rejects_impossible_calendar_days() -> None:
    assert parse("2026-02-30") is None
    assert parse("31/02/2026") is None


def test_find_returns_reading_order_with_offsets() -> None:
    text = "Signed 31/12/1999 and amended 01/01/2000."

    hits = find(text)

    assert [hit["iso"] for hit in hits] == ["1999-12-31", "2000-01-01"]
    assert text[hits[0]["start"] : hits[0]["end"]] == hits[0]["text"]


def test_find_does_not_report_the_same_span_twice() -> None:
    """An ISO token is also 'date shaped' to a looser pattern; it must surface once."""
    hits = find("2026-11-17")

    assert len(hits) == 1
    assert hits[0]["iso"] == "2026-11-17"


def test_find_honours_limit() -> None:
    assert len(find("01/02/2000 03/04/2001 05/06/2002", limit=2)) == 2
    assert find("01/02/2000", limit=0) == []


def test_find_reads_an_iso_date_inside_an_iso_timestamp() -> None:
    hits = find("2026-11-17T00:00:00Z")

    assert [hit["iso"] for hit in hits] == ["2026-11-17"]


def test_non_string_or_empty_input_is_not_a_date() -> None:
    assert parse(None) is None
    assert parse("") is None
    assert find(None) == []
    assert is_date(2026) is False


def test_is_date_is_the_boolean_form_of_parse() -> None:
    assert is_date("17 November 2026") is True
    assert is_date("section 4.2") is False


def test_dateparse_is_bound_in_the_script_sandbox() -> None:
    from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB

    dateparse = _STDLIB["dateparse"]
    for member in ("parse", "find", "is_date"):
        assert callable(getattr(dateparse, member)), member


def test_dateparse_runs_inside_the_script_sandbox_namespace() -> None:
    """End-to-end in the real sandbox: a judgment date is extracted from a
    paragraph and a prose number stays untouched (never a fabricated date)."""
    from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS, run_script_sync

    code = """
paragraphs = ["The court rules as follows.", "Judgment of 17 November 2026", "Section 4.2 applies."]
hits = [dateparse.parse(p) for p in paragraphs]
result = "|".join("" if hit is None else hit for hit in hits)
"""
    result, error = run_script_sync(code, {"__builtins__": SAFE_BUILTINS, **_STDLIB})

    assert error is None
    assert result == "|2026-11-17|"
