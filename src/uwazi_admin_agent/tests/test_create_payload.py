"""Isolated unit tests for :func:`uwazi_admin_agent.domain.create_payload.to_create_payload`.

Pure transform: literal raw dicts in, plain assertions out. No I/O, no mocks.
"""

from uwazi_admin_agent.domain.create_payload import strip_deleted_entity_refs, to_create_payload


def _full_raw() -> dict:
    return {
        "_id": "x1",
        "sharedId": "X",
        "title": "Old X",
        "template": "tmpl_abc",
        "icon": {"_id": "icon1", "label": "Icon", "type": "icon"},
        "user": "user_123",
        "language": "en",
        "metadata": {"caption": [{"value": "hello"}], "date": [{"value": 1700000000}]},
        "attachments": [{"originalname": "doc.pdf", "url": "https://e.com/doc.pdf"}],
        "relations": [{"_id": "r2", "label": "rel"}],
        "documents": [{"_id": "d1", "originalname": "f.pdf"}],
        "generatedToc": True,
        "propertySelections": {"fileID": "d1", "selections": []},
        "published": True,
        "creationDate": 1000,
        "editDate": 1005,
        "file": {"filename": "f.pdf"},
    }


def test_keeps_all_create_accepted_fields() -> None:
    payload = to_create_payload(_full_raw())
    assert payload == {
        "title": "Old X",
        "template": "tmpl_abc",
        "icon": {"_id": "icon1", "label": "Icon", "type": "icon"},
        "user": "user_123",
        "metadata": {"caption": [{"value": "hello"}], "date": [{"value": 1700000000}]},
        "attachments": [{"originalname": "doc.pdf", "url": "https://e.com/doc.pdf"}],
    }


def test_strips_identity_fields() -> None:
    payload = to_create_payload(_full_raw())
    assert "_id" not in payload
    assert "sharedId" not in payload


def test_strips_server_managed_and_unsupported_fields() -> None:
    payload = to_create_payload(_full_raw())
    for dropped in (
        "language",
        "relations",
        "documents",
        "generatedToc",
        "propertySelections",
        "published",
        "creationDate",
        "editDate",
        "file",
    ):
        assert dropped not in payload


def test_omits_absent_accepted_fields_without_error() -> None:
    raw = {"_id": "x1", "sharedId": "X", "title": "Bare"}
    payload = to_create_payload(raw)
    assert payload == {"title": "Bare"}


def test_does_not_mutate_input() -> None:
    raw = _full_raw()
    before = dict(raw)
    to_create_payload(raw)
    assert raw == before


def test_missing_title_yields_empty_title_field_absent() -> None:
    # title is required by CreateEntitySchema but the transform keeps whatever is
    # present; an absent title simply is not included (the live POST would 422 —
    # a loud error, by design). A real entity always has a title.
    raw = {"_id": "x1", "sharedId": "X", "metadata": {}}
    payload = to_create_payload(raw)
    assert "title" not in payload
    assert payload == {"metadata": {}}


# --- strip_deleted_entity_refs (delete-revert: avoid create-branch 400) --------


def test_strip_drops_refs_to_co_deleted_and_self() -> None:
    metadata = {
        "entity_relation": [{"value": "B", "label": "B title"}, {"value": "C", "label": "C title"}],
        "caption": [{"value": "hello"}],
    }
    # deleted_ids = self (A) + co-deleted B; C still exists.
    stripped = strip_deleted_entity_refs(metadata, {"A", "B"})

    assert stripped["entity_relation"] == [{"value": "C", "label": "C title"}]
    # Non-relationship property whose values happen to be {value: ...} but whose
    # value is not a deleted sharedId is preserved unchanged.
    assert stripped["caption"] == [{"value": "hello"}]


def test_strip_preserves_refs_to_still_existing_entities() -> None:
    metadata = {"entity_relation": [{"value": "STATE", "label": "State"}, {"value": "B", "label": "B"}]}
    stripped = strip_deleted_entity_refs(metadata, {"A", "B"})
    assert stripped["entity_relation"] == [{"value": "STATE", "label": "State"}]


def test_strip_drops_all_refs_when_only_co_deleted_targets() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B"}]}
    stripped = strip_deleted_entity_refs(metadata, {"A", "B"})
    assert stripped["entity_relation"] == []


def test_strip_leaves_thesaurus_and_scalar_values_untouched() -> None:
    # Thesaurus/select values are UUIDs (never sharedIds), dates are scalars —
    # these are not {value: sharedId} relationship refs and must pass through.
    # deleted_ids holds only entity sharedIds, so no thesaurus value matches.
    metadata = {
        "status": [{"value": "uuid-123", "label": "Final"}],
        "date": [{"value": 1700000000}],
        "title_scalar": "A plain title",
        "tags": ["x", "y"],
        "empty_rel": [],
    }
    stripped = strip_deleted_entity_refs(metadata, {"A", "B"})
    assert stripped == metadata


def test_strip_does_not_mutate_input() -> None:
    metadata = {"entity_relation": [{"value": "B", "label": "B"}, {"value": "C", "label": "C"}]}
    before = {k: list(v) if isinstance(v, list) else v for k, v in metadata.items()}
    strip_deleted_entity_refs(metadata, {"A", "B"})
    assert metadata == before
    assert metadata["entity_relation"][0]["value"] == "B"  # original entry untouched


def test_strip_handles_missing_metadata() -> None:
    assert strip_deleted_entity_refs({}, {"A"}) == {}
