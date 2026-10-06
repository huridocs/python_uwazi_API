from uwazi_property_filler.domain.label import LabelPrediction
from uwazi_property_filler.use_cases.get_predictions_use_case import dedupe_predictions


def _pred(source: str, label: str, text: str) -> LabelPrediction:
    return LabelPrediction(label_shared_id=label, label_title=label, text=text, source=source, confidence=0.9)


def test_dedupe_drops_identical_predictions() -> None:
    predictions = [_pred("e1", "a", "hello"), _pred("e1", "a", "hello"), _pred("e2", "a", "hello")]
    result = dedupe_predictions(predictions)
    assert len(result) == 2


def test_dedupe_keeps_distinct_label_or_text() -> None:
    predictions = [
        _pred("e1", "a", "hello"),
        _pred("e1", "a", "world"),
        _pred("e1", "b", "hello"),
    ]
    assert len(dedupe_predictions(predictions)) == 3


def test_dedupe_empty() -> None:
    assert dedupe_predictions([]) == []
