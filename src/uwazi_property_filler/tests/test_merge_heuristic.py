from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.use_cases.get_suggestions_use_case import merge_suggestions


def test_single_value_picks_highest_confidence() -> None:
    suggestions = [
        Suggestion(property_name="status", value="A", confidence=0.5, source_extension_id="e1"),
        Suggestion(property_name="status", value="B", confidence=0.9, source_extension_id="e2"),
    ]
    assert merge_suggestions(suggestions) == {"status": "B"}


def test_list_value_dedupes_and_ranks_by_confidence_then_votes() -> None:
    suggestions = [
        Suggestion(property_name="tags", value=["x", "y"], confidence=0.8, source_extension_id="e1"),
        Suggestion(property_name="tags", value=["y", "z"], confidence=0.6, source_extension_id="e2"),
        Suggestion(property_name="tags", value=["x"], confidence=0.8, source_extension_id="e3"),
    ]
    # x: 0.8 conf, 2 votes; y: 0.8 conf, 2 votes; z: 0.6 conf, 1 vote.
    merged = merge_suggestions(suggestions)
    assert merged["tags"] == ["x", "y", "z"]


def test_list_value_vote_count_breaks_confidence_ties() -> None:
    suggestions = [
        Suggestion(property_name="tags", value=["a"], confidence=0.7, source_extension_id="e1"),
        Suggestion(property_name="tags", value=["b"], confidence=0.7, source_extension_id="e1"),
        Suggestion(property_name="tags", value=["a"], confidence=0.7, source_extension_id="e2"),
    ]
    # a has 2 agreeing extensions; b has 1. Both 0.7 confidence.
    assert merge_suggestions(suggestions)["tags"] == ["a", "b"]


def test_empty_input_returns_empty_dict() -> None:
    assert merge_suggestions([]) == {}


def test_mixed_single_and_list_properties() -> None:
    suggestions = [
        Suggestion(property_name="title", value="Doc", confidence=0.3, source_extension_id="e1"),
        Suggestion(property_name="tags", value=["x"], confidence=0.5, source_extension_id="e1"),
        Suggestion(property_name="title", value="Better", confidence=0.9, source_extension_id="e2"),
    ]
    assert merge_suggestions(suggestions) == {"title": "Better", "tags": ["x"]}
