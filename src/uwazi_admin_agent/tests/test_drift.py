"""Isolated tests for the drift domain (pure functions, no Uwazi, no mocks)."""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.drift import (
    DRIFT_LABEL,
    DriftEntry,
    DriftKind,
    DriftStatus,
    build_probe_result,
    build_report,
    max_live_edit_date,
)
from uwazi_admin_agent.drivers.web.review_view import (
    DRIFT_STATUS_COLORS,
    drift_banner_text,
    drift_banner_time,
    drift_text_classes,
)
from uwazi_admin_agent.drivers.web.run_service import _drift_detail


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


def test_report_includes_file_drift_entries() -> None:
    """A file-only change cannot move editDate, so it arrives as its own kind."""
    file_entry = DriftEntry(shared_id="source", kind=DriftKind.FILES)

    report = build_report(
        live=[
            {"shared_id": "kept", "edit_date": 10},
            {"shared_id": "source", "edit_date": 10},
        ],
        baseline={"kept": 10, "source": 10},
        excluded_ids=set(),
        languages_failed=[],
        checked_at=_now(),
        files=[file_entry],
    )

    assert report.modified == []
    assert [entry.shared_id for entry in report.files] == ["source"]
    assert [entry.shared_id for entry in report.entries()] == ["source"]
    assert report.total() == 1
    assert DRIFT_LABEL[DriftKind.FILES] == "Files changed in Uwazi"


def test_a_source_modified_and_file_drifted_is_listed_once() -> None:
    report = build_report(
        live=[{"shared_id": "source", "edit_date": 99}],
        baseline={"source": 10},
        excluded_ids=set(),
        languages_failed=[],
        checked_at=_now(),
        files=[DriftEntry(shared_id="source", kind=DriftKind.FILES)],
    )

    assert [entry.shared_id for entry in report.modified] == ["source"]
    assert report.files == []
    assert report.total() == 1


def test_drift_detail_names_file_drift_as_entities_not_files() -> None:
    report = build_report(
        live=[
            {"shared_id": "a", "edit_date": 99},
            {"shared_id": "files-1", "edit_date": 10},
            {"shared_id": "files-2", "edit_date": 10},
        ],
        baseline={"a": 10, "files-1": 10, "files-2": 10},
        excluded_ids=set(),
        languages_failed=[],
        checked_at=_now(),
        files=[
            DriftEntry(shared_id="files-1", kind=DriftKind.FILES),
            DriftEntry(shared_id="files-2", kind=DriftKind.FILES),
        ],
    )

    assert _drift_detail(report) == "1 modified in uwazi, 2 entities with changed files in uwazi"


def test_drift_detail_uses_the_singular_for_one_file_drifted_entity() -> None:
    report = build_report(
        live=[{"shared_id": "files-1", "edit_date": 10}],
        baseline={"files-1": 10},
        excluded_ids=set(),
        languages_failed=[],
        checked_at=_now(),
        files=[DriftEntry(shared_id="files-1", kind=DriftKind.FILES)],
    )

    assert _drift_detail(report) == "1 entity with changed files in uwazi"


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
    assert DRIFT_STATUS_COLORS[DriftStatus.DRIFTED.value] == "red"
    # the OUT OF DATE message itself is red, not just its icon
    assert drift_text_classes(DRIFT_STATUS_COLORS[DriftStatus.DRIFTED.value]) == "text-body2 text-red grow"
    # the resting state stays grey
    assert drift_text_classes(DRIFT_STATUS_COLORS[DriftStatus.UNKNOWN.value]) == "text-body2 text-grey-7 grow"
    # UP TO DATE is neutral grey too, not green: only drift demands attention
    assert drift_text_classes(DRIFT_STATUS_COLORS[DriftStatus.VALID.value]) == "text-body2 text-grey-7 grow"


def test_banner_time_formats() -> None:
    assert drift_banner_time(None) == ""
    assert drift_banner_time(datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc)) == " · checked 09:05"


def test_report_revert_of_a_delete_is_not_drift() -> None:
    """A reverted DELETE re-creates the entity under a NEW sharedId.

    Without the manifest's old→new alias the run's own undo reads as "1 new in
    uwazi, 1 deleted in uwazi" forever — the operator's own revert reported
    back to them as someone else's change.
    """
    report = build_report(
        live=[{"shared_id": "restored-2", "edit_date": 500}],
        baseline={"deleted-1": 10},
        excluded_ids={"deleted-1", "restored-2"},
        languages_failed=[],
        checked_at=_now(),
        aliases={"deleted-1": "restored-2"},
    )

    assert report.total() == 0
    assert _drift_detail(report) == ""


def test_report_still_flags_a_deletion_when_the_alias_vanished() -> None:
    """The alias explains ONE id; if the re-created entity is gone too, say so."""
    report = build_report(
        live=[],
        baseline={"deleted-1": 10},
        excluded_ids={"deleted-1"},
        languages_failed=[],
        checked_at=_now(),
        aliases={"deleted-1": "restored-2"},
    )

    assert [entry.shared_id for entry in report.missing] == ["deleted-1"]


def test_report_never_reports_the_runs_own_deletes_as_missing() -> None:
    """A Delete task applied from this very table deleted those entities itself.

    Reporting them as "Deleted in Uwazi" showed the operator their own deletion
    as an outside edit — the drift banner lit up right after a successful Run.
    """
    report = build_report(
        live=[{"shared_id": "kept", "edit_date": 10}],
        baseline={"kept": 10, "we-deleted-this": 10},
        excluded_ids={"we-deleted-this"},
        languages_failed=[],
        checked_at=_now(),
        removed_by_run={"we-deleted-this"},
    )

    assert report.missing == []
    assert report.total() == 0


def test_report_still_flags_an_entity_we_only_modified_and_uwazi_deleted() -> None:
    """Only the manifest's DELETED set is excused; a modified entity that later
    vanishes is real drift."""
    report = build_report(
        live=[],
        baseline={"we-modified": 10},
        excluded_ids={"we-modified"},
        languages_failed=[],
        checked_at=_now(),
        removed_by_run=set(),
    )

    assert [entry.shared_id for entry in report.missing] == ["we-modified"]


def test_run_removed_ids_covers_deletes_and_already_reverted_creates() -> None:
    """A reverted create is undone by DELETING it, so it is gone for the same
    reason a deleted entity is — the run's own write, not drift."""
    from types import SimpleNamespace

    from uwazi_admin_agent.use_cases.drift_service import run_removed_ids

    manifest = SimpleNamespace(
        deleted=[SimpleNamespace(shared_id="deleted-1")],
        created=[SimpleNamespace(shared_id="created-1"), SimpleNamespace(shared_id="created-2")],
        reverted_shared_ids=["created-1"],
    )

    assert run_removed_ids(manifest) == {"deleted-1", "created-1"}


class _SyncProbe:
    """A real (non-mock) drift probe over a literal template scan."""

    def __init__(self, fail_languages: frozenset[str] = frozenset()) -> None:
        self._fail = fail_languages

    def scan_template(self, template_id: str, language: str) -> dict[str, int | None]:
        if language in self._fail:
            raise RuntimeError(f"scan failed for {language}")
        return {"a": 100}

    def count_by_template(self, template_id: str) -> int:
        return 1


def _store_with_rows(tmp_path, languages: tuple[str, ...]):
    from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
    from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus

    store = SqliteSuggestionStore(tmp_path / "suggestions.db")
    store.add_suggestions(
        [
            Suggestion(
                id=index,
                run_id="run-1",
                shared_id="a",
                template_name="Doc",
                op="update",
                property_name=f"p{index}",
                language=language,
                proposed_value="v",
                status=SuggestionStatus.PENDING,
            )
            for index, language in enumerate(languages, start=1)
        ]
    )
    return store


def test_a_complete_baseline_sync_reads_as_up_to_date(tmp_path) -> None:
    """After a Run/Revert the table matches the baseline just captured, so the
    banner must say "Up to date" — it used to persist UNKNOWN ("Drift not checked
    yet" with an internal "baseline synced" caption) and the stale red
    "Out of date" verdict stayed on screen."""
    from uwazi_admin_agent.domain.drift import DriftStatus
    from uwazi_admin_agent.use_cases import drift_service

    store = _store_with_rows(tmp_path, ("en",))

    assert drift_service.sync_drift_baseline(store, _SyncProbe(), lambda _name: "tpl-1", "run-1") == 1
    state = store.drift_state("run-1")

    assert state is not None
    assert state.status is DriftStatus.VALID
    assert state.detail is None
    assert state.live_count == 1


def test_a_partial_baseline_capture_never_claims_up_to_date(tmp_path) -> None:
    """One failed language leaves a hole in the baseline: the verdict is
    UNKNOWN naming it, never VALID over data the capture never saw."""
    from uwazi_admin_agent.domain.drift import DriftStatus
    from uwazi_admin_agent.use_cases import drift_service

    store = _store_with_rows(tmp_path, ("en", "es"))

    drift_service.sync_drift_baseline(store, _SyncProbe(frozenset({"es"})), lambda _name: "tpl-1", "run-1")
    state = store.drift_state("run-1")

    assert state is not None
    assert state.status is DriftStatus.UNKNOWN
    assert state.detail == "baseline incomplete (es)"
