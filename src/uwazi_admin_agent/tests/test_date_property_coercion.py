"""Isolated unit tests for DATE-typed property value coercion (per AGENTS.md).

Pure: literal values in, Uwazi shapes out. No network, no mocks, no instance.

The extraction tasks cooked this up the hard way: a published date recorded as
``"2026-10-30"``, pushed raw by the property-scoped write path, was a guaranteed
500 on ``POST /api/entities/multipleupdate`` (and a ``422 Expected array,
received string`` on the per-entity fallback). The value must reach Uwazi as
``[seconds]`` — while the review table renders that shape back as
``YYYY-MM-DD`` (see ``uwazi_admin_agent.domain.metadata_diff``, whose
``_timestamp_to_date`` accepts the same seconds and numbers this module
coerces TO).
"""

from datetime import date, datetime, timezone

from uwazi_admin_agent.domain.date_values import coerce_date_property_value, is_date_property_type
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionStatus
from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import build_apply_updates


def _epoch(iso: str) -> int:
    """The epoch-seconds value an ISO date string coerces to (mirror of the rules)."""
    return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp())


# --- is_date_property_type ----------------------------------------------------


def test_date_type_recognition() -> None:
    assert is_date_property_type("date")
    assert is_date_property_type("multidate")
    assert is_date_property_type("daterange")
    assert is_date_property_type("multidaterange")
    assert not is_date_property_type("text")
    assert not is_date_property_type("select")
    assert not is_date_property_type(None)
    assert not is_date_property_type("")


# --- date / multidate ----------------------------------------------------------


def test_iso_string_becomes_epoch_seconds_array() -> None:
    assert coerce_date_property_value("2026-10-30", "date") == [_epoch("2026-10-30")]


def test_iso_datetime_string_becomes_epoch_seconds_array() -> None:
    value = coerce_date_property_value("2020-05-01T00:00:00", "date")
    assert value == [_epoch("2020-05-01")]


def test_numeric_timestamps_pass_through_unchanged() -> None:
    seconds = _epoch("2026-10-30")
    milliseconds = seconds * 1000
    assert coerce_date_property_value(seconds, "date") == [seconds]
    assert coerce_date_property_value(milliseconds, "date") == [milliseconds]
    assert coerce_date_property_value(float(milliseconds), "date") == [float(milliseconds)]


def test_numeric_string_timestamp_passes_through() -> None:
    # A legacy/imported value stored as a numeric STRING is already a
    # timestamp — accepted, not parsed as a date, not dropped.
    assert coerce_date_property_value(str(_epoch("2026-10-30")), "date") == [_epoch("2026-10-30")]


def test_datetime_and_date_objects_become_epochs() -> None:
    moment = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    naive = datetime(2026, 10, 30)
    assert coerce_date_property_value(moment, "date") == [int(moment.timestamp())]
    # A naive datetime is interpreted in UTC (a local clock must not move the date).
    assert coerce_date_property_value(naive, "date") == [_epoch("2026-10-30")]
    assert coerce_date_property_value(date(2026, 10, 30), "date") == [_epoch("2026-10-30")]


def test_list_of_iso_strings_becomes_epoch_list() -> None:
    multidate = coerce_date_property_value(["2020-01-01", "2021-06-15"], "multidate")
    assert multidate == [_epoch("2020-01-01"), _epoch("2021-06-15")]


def test_empty_and_none_collapse_to_empty_array() -> None:
    assert coerce_date_property_value(None, "date") == []
    assert coerce_date_property_value("", "date") == []


def test_unparseable_date_string_passes_through_for_a_loud_refusal() -> None:
    # Mangling prose into an epoch would write a 1970 date. The string is kept
    # (wrapped in the array Uwazi's schema asks for) so the REFUSAL names the
    # value itself instead of blaming the type: the write fails loudly with
    # Uwazi's own "invalid date", with no silent rewriting.
    assert coerce_date_property_value("sometime in the 80s", "date") == ["sometime in the 80s"]


def test_envelope_wrapped_string_is_unwrapped() -> None:
    legacy = [{"value": "2020-05-01"}]
    assert coerce_date_property_value(legacy, "date") == [_epoch("2020-05-01")]


# --- daterange / multidaterange ------------------------------------------------


def test_range_dict_becomes_epoch_range() -> None:
    rng = coerce_date_property_value({"from": "2020-01-01", "to": "2020-12-31"}, "daterange")
    assert rng == {"from": _epoch("2020-01-01"), "to": _epoch("2020-12-31")}


def test_arrow_string_becomes_epoch_range() -> None:
    rng = coerce_date_property_value("2020-01-01->2020-12-31", "daterange")
    assert rng == {"from": _epoch("2020-01-01"), "to": _epoch("2020-12-31")}


def test_multidaterange_list_becomes_range_list() -> None:
    ranges = coerce_date_property_value(
        [{"from": "2020-01-01", "to": "2020-02-01"}, {"from": "2021-01-01", "to": None}],
        "multidaterange",
    )
    assert ranges == [
        {"from": _epoch("2020-01-01"), "to": _epoch("2020-02-01")},
        {"from": _epoch("2021-01-01")},
    ]


def test_open_ended_range_keeps_only_from() -> None:
    rng = coerce_date_property_value({"from": "2020-01-01"}, "daterange")
    assert rng == {"from": _epoch("2020-01-01")}


def test_unparseable_range_passes_through() -> None:
    assert coerce_date_property_value("not a range", "daterange") == "not a range"


# --- recording: records_to_suggestions normalizes date values -------------------


def _update_record(shared_id: str, metadata: dict, template_name: str = "Reports") -> dict:
    return {"op": "update", "shared_id": shared_id, "template_name": template_name, "language": None, "metadata": metadata}


def test_recording_coerces_a_date_string_into_epoch_array() -> None:
    suggestions = records_to_suggestions(
        "run1",
        [_update_record("s1", {"date_published": "2026-10-30"})],
        property_types={"date_published": "date"},
    )
    rows = [s for s in suggestions if s.property_name == "date_published"]
    assert rows and rows[0].proposed_value == [_epoch("2026-10-30")]


def test_recording_without_types_keeps_the_script_value() -> None:
    suggestions = records_to_suggestions("run1", [_update_record("s1", {"date_published": "2026-10-30"})])
    rows = [s for s in suggestions if s.property_name == "date_published"]
    assert rows and rows[0].proposed_value == "2026-10-30"


def test_recording_leaves_non_date_values_alone() -> None:
    suggestions = records_to_suggestions(
        "run1",
        [_update_record("s1", {"date_published": "2026-10-30", "notes": "same text"})],
        property_types={"date_published": "date", "notes": "text"},
    )
    by_name = {s.property_name: s.proposed_value for s in suggestions if s.property_name}
    assert by_name["date_published"] == [_epoch("2026-10-30")]
    assert by_name["notes"] == "same text"


# --- apply: build_apply_updates coerces at the payload boundary -----------------


def test_apply_payload_coerces_a_legacy_recorded_date_string() -> None:
    # A pre-existing row (recorded before the normalization existed) must be
    # fixed at the write boundary — this is what un-blocks the review table
    # the extract-published-date task left behind.
    row = Suggestion(
        run_id="run1",
        shared_id="s4fcs3o0ewp",
        language=None,
        op="update",  # type: ignore[arg-type]
        property_name="date_published",
        proposed_value="2026-10-30",
        status=SuggestionStatus.ACCEPTED,
    )
    updates = build_apply_updates([row], property_types={"date_published": "date"})
    assert updates == [
        {
            "shared_id": "s4fcs3o0ewp",
            "language": None,
            "metadata": {"date_published": [_epoch("2026-10-30")]},
        }
    ]


def test_apply_payload_without_types_keeps_the_recorded_value() -> None:
    row = Suggestion(
        run_id="run1",
        shared_id="s1",
        language=None,
        op="update",  # type: ignore[arg-type]
        property_name="mystery_prop",
        proposed_value="2026-10-30",
        status=SuggestionStatus.ACCEPTED,
    )
    updates = build_apply_updates([row])
    assert updates[0]["metadata"]["mystery_prop"] == "2026-10-30"


def test_apply_payload_coerces_an_operator_edit_for_a_date_property() -> None:
    # The Edit flow stores its patch as a JSON-encoded value; a date typed by
    # the operator gets the same treatment as the script's own proposal.
    row = Suggestion(
        run_id="run1",
        shared_id="s1",
        language=None,
        op="update",  # type: ignore[arg-type]
        property_name="date_published",
        proposed_value="2020-05-01",
        edited_value='"2020-06-01"',
        status=SuggestionStatus.EDITED,
    )
    updates = build_apply_updates([row], property_types={"date_published": "date"})
    assert updates[0]["metadata"]["date_published"] == [_epoch("2020-06-01")]
