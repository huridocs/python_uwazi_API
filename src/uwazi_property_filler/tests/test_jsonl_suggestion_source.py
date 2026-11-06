"""Isolated unit tests for the JSONL suggestion row parser (no I/O, no mocks)."""

from pathlib import Path

from uwazi_property_filler.adapters.jsonl_suggestion_source import (
    JsonlSuggestionSource,
    parse_suggestion_line,
    shared_id_from_id,
)


def test_shared_id_uses_the_suffix_after_the_property_id() -> None:
    assert shared_id_from_id("qt7fau6bu7p:6a75c5bad410d2e28bd5e59c") == "6a75c5bad410d2e28bd5e59c"


def test_shared_id_without_a_colon_is_returned_as_is() -> None:
    assert shared_id_from_id("6a75c5bad410d2e28bd5e59c") == "6a75c5bad410d2e28bd5e59c"


def test_shared_id_empty_values() -> None:
    assert shared_id_from_id(None) == ""
    assert shared_id_from_id("  ") == ""


def test_parse_suggestion_line_reads_predicted_labels() -> None:
    parsed = parse_suggestion_line(
        {
            "id": "property-id:entity-shared-id",
            "predicted_labels": ["Best interests of the child", "Family life"],
        },
        source="model-a",
        default_property="topic",
    )
    assert parsed is not None
    shared_id, suggestion = parsed
    assert shared_id == "entity-shared-id"
    assert suggestion.property_name == "topic"
    assert suggestion.value == ["Best interests of the child", "Family life"]
    assert suggestion.source_extension_id == "model-a"
    assert suggestion.confidence == 1.0


def test_parse_suggestion_line_accepts_a_single_label() -> None:
    parsed = parse_suggestion_line(
        {"id": "p:e", "predicted_labels": "Family"},
        source="model-a",
        default_property="topic",
    )
    assert parsed is not None
    assert parsed[1].value == ["Family"]


def test_parse_suggestion_line_honors_row_overrides() -> None:
    parsed = parse_suggestion_line(
        {
            "id": "p:e",
            "predicted_labels": ["Family"],
            "source": "model-b",
            "property_name": "keywords",
            "confidence": 0.75,
        },
        source="file-stem",
        default_property="topic",
    )
    assert parsed is not None
    _, suggestion = parsed
    assert suggestion.source_extension_id == "model-b"
    assert suggestion.property_name == "keywords"
    assert suggestion.confidence == 0.75


def test_parse_suggestion_line_dedupes_labels_and_drops_empty_ones() -> None:
    parsed = parse_suggestion_line(
        {"id": "p:e", "predicted_labels": ["Family", "  Family  ", "", None, "Identity"]},
        source="model-a",
        default_property="topic",
    )
    assert parsed is not None
    assert parsed[1].value == ["Family", "Identity"]


def test_parse_suggestion_line_returns_none_without_labels_or_id() -> None:
    assert parse_suggestion_line({"id": "p:e", "predicted_labels": []}, source="s", default_property="p") is None
    assert parse_suggestion_line({"id": "p:e"}, source="s", default_property="p") is None
    assert parse_suggestion_line({"predicted_labels": ["Family"]}, source="s", default_property="p") is None


def test_source_reads_every_jsonl_file_of_every_directory(tmp_path: Path) -> None:
    packaged = tmp_path / "packaged"
    runtime = tmp_path / "runtime"
    packaged.mkdir()
    runtime.mkdir()
    (packaged / "model_a.jsonl").write_text(
        '{"id": "p:e1", "predicted_labels": ["Family"]}\n', encoding="utf-8"
    )
    (runtime / "model_b.jsonl").write_text(
        '{"id": "p:e2", "predicted_labels": ["Identity"]}\n', encoding="utf-8"
    )

    source = JsonlSuggestionSource([packaged, runtime], "topic")

    assert [path.name for path in source.files()] == ["model_a.jsonl", "model_b.jsonl"]
    loaded = source._load()  # noqa: SLF001 — the sync loader is the unit under test
    assert [shared_id for shared_id, _ in loaded] == ["e1", "e2"]
    assert loaded[0][1].value == ["Family"]
    assert loaded[0][1].source_extension_id == "model_a"
    assert loaded[1][1].source_extension_id == "model_b"


def test_source_ignores_directories_that_do_not_exist(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "model_a.jsonl").write_text(
        '{"id": "p:e1", "predicted_labels": ["Family"]}\n', encoding="utf-8"
    )

    source = JsonlSuggestionSource([tmp_path / "missing", existing], "topic")

    assert [path.name for path in source.files()] == ["model_a.jsonl"]
    assert [shared_id for shared_id, _ in source._load()] == ["e1"]


def test_source_accepts_a_single_directory(tmp_path: Path) -> None:
    (tmp_path / "model_a.jsonl").write_text(
        '{"id": "p:e1", "predicted_labels": ["Family"]}\n', encoding="utf-8"
    )

    source = JsonlSuggestionSource(tmp_path, "topic")

    assert [path.name for path in source.files()] == ["model_a.jsonl"]
    assert [shared_id for shared_id, _ in source._load()] == ["e1"]
