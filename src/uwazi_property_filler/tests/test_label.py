from uwazi_property_filler.domain.label import MANUAL_SOURCE, Label, LabelPrediction
from uwazi_property_filler.use_cases.label_use_case import labels_from_relations, object_id


def test_label_defaults_to_manual() -> None:
    label = Label(shared_id="doc", label_shared_id="lbl")
    assert label.source == MANUAL_SOURCE
    assert label.is_manual is True
    assert label.confidence is None
    assert label.selection_rectangles == []


def test_prediction_is_not_manual() -> None:
    pred = LabelPrediction(label_shared_id="lbl", source="ext1", confidence=0.8)
    assert pred.source == "ext1"
    assert pred.confidence == 0.8


def test_object_id_accepts_hex_string() -> None:
    assert object_id("abcdef0123456789abcdef01") == "abcdef0123456789abcdef01"


def test_object_id_accepts_extended_json() -> None:
    assert object_id({"$oid": "abcdef0123456789abcdef01"}) == "abcdef0123456789abcdef01"


def test_object_id_rejects_unexpected() -> None:
    assert object_id(None) is None
    assert object_id({}) is None
    assert object_id(123) is None


_RELATIONS = [
    # FROM row for hub "h1" (the document side, carrying file + reference).
    {
        "hub": "h1",
        "template": None,
        "entity": "pdfid",
        "file": "file1",
        "reference": {
            "text": "hello world",
            "selectionRectangles": [{"top": 100.0, "left": 10.0, "width": 20.0, "height": 10.0, "page": "1"}],
        },
    },
    # TO row for hub "h1" (the label target).
    {"hub": "h1", "template": "reltype1", "entity": "labelid1"},
    # A hub whose FROM cites a DIFFERENT file (must be ignored).
    {
        "hub": "h2",
        "template": None,
        "entity": "pdfid",
        "file": "otherfile",
        "reference": {"text": "other", "selectionRectangles": []},
    },
    {"hub": "h2", "template": "reltype1", "entity": "labelid2"},
]


def test_labels_from_relations_filters_to_this_file() -> None:
    labels = labels_from_relations(_RELATIONS, shared_id="pdfid", file_id="file1", relationship_type_id="reltype1")
    assert len(labels) == 1
    label = labels[0]
    assert label.hub == "h1"
    assert label.label_shared_id == "labelid1"
    assert label.text == "hello world"
    assert label.file_id == "file1"
    assert len(label.selection_rectangles) == 1
    assert label.is_manual is True


def test_labels_from_relations_matches_type_only() -> None:
    # Same relations but a mismatched relationship type → no labels.
    labels = labels_from_relations(_RELATIONS, shared_id="pdfid", file_id="file1", relationship_type_id="othertype")
    assert labels == []


def test_labels_from_relations_extended_json_hub() -> None:
    relations = [
        {
            "hub": {"$oid": "h1"},
            "template": None,
            "entity": "pdfid",
            "file": {"$oid": "file1"},
            "reference": {"text": "x", "selectionRectangles": []},
        },
        {"hub": {"$oid": "h1"}, "template": "reltype1", "entity": "labelid1"},
    ]
    labels = labels_from_relations(relations, shared_id="pdfid", file_id="file1", relationship_type_id="reltype1")
    assert len(labels) == 1
    assert labels[0].hub == "h1"
