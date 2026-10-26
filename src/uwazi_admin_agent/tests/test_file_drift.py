"""Isolated tests for targeted file-set drift (pure, no Uwazi, no mocks)."""

from types import SimpleNamespace

from uwazi_admin_agent.domain.file_drift import (
    changed_file_entities,
    file_identity,
    file_signature,
    recorded_file_languages,
    recorded_file_signatures,
)


def _file(**kwargs: object) -> dict[str, object]:
    return dict(kwargs)


def _row(sources: list[str], files: list[dict[str, object]]) -> SimpleNamespace:
    return SimpleNamespace(merge_source_ids=sources, merge_files=files)


def test_file_identity_prefers_the_file_id_and_keeps_the_kind() -> None:
    assert file_identity({"kind": "attachment", "file_id": "f1", "filename": "stored"}) == (
        "attachment",
        "f1",
    )
    # A replacement upload mints a new id, so the identity changes.
    assert file_identity({"kind": "document", "filename": "stored"}) == ("document", "stored")
    assert file_identity({"originalname": "report.pdf"}) == ("", "report.pdf")
    assert file_identity({"kind": "attachment"}) == ("attachment", "")


def test_file_signature_is_an_unordered_set_ignoring_nameless_entries() -> None:
    files = [
        _file(kind="document", file_id="d1"),
        _file(kind="attachment", file_id="a1"),
        _file(kind="attachment"),
    ]

    assert file_signature(files) == frozenset({("document", "d1"), ("attachment", "a1")})


def test_recorded_signatures_group_files_by_source_and_keep_empty_sources() -> None:
    rows = [
        _row(
            ["S1", "S2"],
            [
                _file(kind="document", file_id="d1", from_shared_id="S1"),
                _file(kind="attachment", file_id="a1", from_shared_id="S2"),
            ],
        ),
        # The target's property rows repeat the same snapshot: it dedupes.
        _row(
            ["S1", "S2"],
            [_file(kind="document", file_id="d1", from_shared_id="S1")],
        ),
        # A standalone move row into a source-less snapshot.
        _row(["S3"], []),
    ]

    assert recorded_file_signatures(rows) == {
        "S1": frozenset({("document", "d1")}),
        "S2": frozenset({("attachment", "a1")}),
        "S3": frozenset(),
    }


def test_recorded_signatures_attribute_a_legacy_file_to_the_only_source() -> None:
    rows = [_row(["S1"], [_file(kind="document", file_id="d1")])]

    assert recorded_file_signatures(rows) == {"S1": frozenset({("document", "d1")})}


def test_recorded_languages_name_the_rows_the_files_were_read_from() -> None:
    rows = [
        _row(
            ["S1", "S2"],
            [
                _file(kind="document", file_id="d1", from_shared_id="S1", language="en"),
                _file(kind="document", file_id="d2", from_shared_id="S1", language="es"),
                _file(kind="attachment", file_id="a1", from_shared_id="S2"),
            ],
        ),
    ]

    # A language-less file (legacy snapshot) contributes no row: the source
    # falls back to the default language instead of guessing.
    assert recorded_file_languages(rows) == {"S1": frozenset({"en", "es"})}


def test_recorded_languages_omits_sources_without_recorded_files() -> None:
    rows = [_row(["S1"], []), _row(["S2"], [_file(kind="document", file_id="d1", language="es")])]

    assert recorded_file_languages(rows) == {"S2": frozenset({"es"})}


def test_changed_file_entities_detects_new_and_removed_files() -> None:
    recorded = {
        "S1": frozenset({("document", "d1")}),
        "S2": frozenset({("attachment", "a1")}),
        "S3": frozenset(),
    }
    live = {
        "S1": [_file(kind="document", file_id="d1"), _file(kind="attachment", file_id="new")],
        "S2": [],
        "S3": [_file(kind="attachment", file_id="first")],
    }

    assert changed_file_entities(recorded, live) == ["S1", "S2", "S3"]


def test_changed_file_entities_keeps_the_recorded_order() -> None:
    recorded = {
        "S2": frozenset({("document", "d1")}),
        "S1": frozenset({("document", "d2")}),
    }
    live = {
        "S1": [_file(kind="document", file_id="d2")],
        "S2": [_file(kind="document", file_id="d1"), _file(kind="attachment", file_id="new")],
    }

    assert changed_file_entities(recorded, live) == ["S2"]
