"""Isolated unit tests for :func:`uwazi_admin_agent.domain.create_payload.to_create_payload`.

Pure transform: literal raw dicts in, plain assertions out. No I/O, no mocks.
"""

from uwazi_admin_agent.domain.create_payload import to_create_payload


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
