"""Isolated tests for the drift domain (pure functions, no Uwazi, no mocks)."""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.drift import (
    DriftKind,
    DriftStatus,
    build_probe_result,
    build_report,
    max_live_edit_date,
)
from uwazi_admin_agent.drivers.web.review_view import DRIFT_STATUS_COLORS, drift_banner_text, drift_banner_time


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_probe_result_flags_only_newer_dates() -> None:
    probed = {"a": 100, "b": None, "c": 50, "d": 200}
    baseline = {"a": 100, "b": None, "c": 100, "d": 100}
    entries = build_probe_result(probed, baseline, excluded_ids=set())
    assert [(e.shared_id, e.kind) for e in entries] == [("d", DriftKind.MODIFIED)]
    assert entries[0].edit_date == 200


def test_probe_result_excludes_run_written_ids() -> None:
    probed = {"a": 999, "run-written": 5000}
    baseline = {"a": 10, "run-written": 10}
    entries = build_probe_result(probed, baseline, excluded_ids={"run-written"})
    assert [e.shared_id for e in entries] == ["a"]


def test_probe_result_ignores_unknown_baseline() -> None:
    """A baseline never seen (None) has no drift knowledge — never flagged."""
    probed = {"a": 999}
    baseline = {"a": None}
    assert build_probe_result(probed, baseline, excluded_ids=set()) == []


def test_report_classifies_modified_new_missing() -> None:
    live = [
        {"shared_id": "kept", "edit_date": 10},
        {"shared_id": "touched", "edit_date": 99},
        {"shared_id": "brand-new", "edit_date": 7},
    ]
    baseline = {"kept": 10, "touched": 20, "deleted": 5}
    report = build_report(live, baseline, excluded_ids=set(), languages_failed=[], checked_at=_now())
    assert [e.shared_id for e in report.modified] == ["touched"]
    assert [e.shared_id for e in report.new] == ["brand-new"]
    assert [e.shared_id for e in report.missing] == ["deleted"]
    assert report.total() == 3
    assert [e.shared_id for e in report.entries()] == ["touched", "brand-new", "deleted"]


def test_report_never_flags_deleted_partial_scan() -> None:
    baseline = {"was-there": 5}
    report = build_report([], baseline, set(), languages_failed=["es"], checked_at=_now())
    assert report.missing == []
    assert report.total() == 0


def test_report_run_written_id_not_modified() -> None:
    live = [{"shared_id": "ours", "edit_date": 999}]
    baseline = {"ours": 10}
    report = build_report(live, baseline, excluded_ids={"ours"}, languages_failed=[], checked_at=_now())
    assert report.modified == []


def test_max_live_edit_date_picks_largest() -> None:
    probed = {"a": 3, "b": 42, "c": None}
    assert max_live_edit_date(probed) == 42
    assert max_live_edit_date({}) is None


def test_banner_texts() -> None:
    assert drift_banner_text(status=DriftStatus.UNKNOWN.value, detail=None) == "Drift not checked yet"
    assert drift_banner_text(status=DriftStatus.VALID.value, detail=None) == (
        "Up to date — no changes in Uwazi since the last sync"
    )
    assert drift_banner_text(status=DriftStatus.DRIFTED.value, detail="3 modified in uwazi") == (
        "Out of date — Uwazi changed since the last sync — 3 modified in uwazi"
    )
    assert drift_banner_text(status=DriftStatus.ERROR.value, detail="boom") == "Drift check failed — boom"
    # checking wins over any verdict
    assert drift_banner_text(status=DriftStatus.VALID.value, detail=None, checking=True) == "Checking drift…"
    # the banner color map covers every verdict
    assert DRIFT_STATUS_COLORS[DriftStatus.DRIFTED.value] == "orange"


def test_banner_time_formats() -> None:
    assert drift_banner_time(None) == ""
    assert drift_banner_time(datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc)) == " · checked 09:05"
