"""Isolated unit tests for the pure delete-revert file-restore transforms.

No mocks, no network — literal snapshot raws + plain assertions. Covers
:func:`extract_file_refs` (documents + uploaded attachments extracted; URL
attachments skipped; content-type derivation; missing-field handling) and
:func:`build_file_restore_actions` (documents-then-attachments ordering, field
mapping, immutability).
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.file_restore import build_file_restore_actions, extract_file_refs
from uwazi_admin_agent.domain.snapshot import FileRef

# --- extract_file_refs ------------------------------------------------------


def test_extract_file_refs_pulls_documents_and_uploaded_attachments() -> None:
    raw = {
        "language": "en",
        "documents": [
            {"_id": "d1", "originalname": "report.pdf", "filename": "hash-d1", "language": "eng", "type": "document"},
        ],
        "attachments": [
            {"_id": "a1", "originalname": "scan.png", "filename": "hash-a1", "type": "attachment"},
            {"_id": "a2", "originalname": "contract", "url": "https://example.com/c.pdf", "type": "attachment"},
        ],
    }

    refs = extract_file_refs(raw)

    assert [r.file_id for r in refs] == ["d1", "a1"]
    doc, att = refs
    assert doc.kind == "document"
    assert doc.originalname == "report.pdf"
    assert doc.filename == "hash-d1"
    assert doc.language == "en"  # entity row language (ISO 639-1) for the upload cookie
    assert doc.content_type == "application/pdf"  # documents are always PDF
    assert att.kind == "attachment"
    assert att.originalname == "scan.png"
    assert att.content_type == "image/png"  # derived from the .png extension


def test_extract_file_refs_skips_url_attachments() -> None:
    raw = {
        "language": "en",
        "attachments": [
            {"_id": "u1", "originalname": "link", "url": "https://example.com/x"},
        ],
        "documents": [],
    }

    assert extract_file_refs(raw) == []


def test_extract_file_refs_skips_entries_missing_id_or_originalname() -> None:
    raw = {
        "language": "en",
        "documents": [
            {"originalname": "no-id.pdf"},  # missing _id
            {"_id": "d2"},  # missing originalname
            {"_id": "", "originalname": "blank-id.pdf"},
        ],
        "attachments": [
            {"_id": "a1", "url": "https://example.com"},  # url attachment
            {"originalname": "no-id-att"},  # missing _id
        ],
    }

    assert extract_file_refs(raw) == []


def test_extract_file_refs_handles_missing_arrays() -> None:
    raw = {"language": "en", "title": "no files here"}

    assert extract_file_refs(raw) == []


def test_extract_file_refs_does_not_mutate_raw() -> None:
    raw = {
        "language": "en",
        "documents": [{"_id": "d1", "originalname": "r.pdf", "filename": "h", "type": "document"}],
        "attachments": [],
    }
    before = {k: (list(v) if isinstance(v, list) else v) for k, v in raw.items()}

    extract_file_refs(raw)

    assert raw == before


def test_extract_file_refs_content_type_derivation_for_attachments() -> None:
    raw = {
        "language": "en",
        "documents": [],
        "attachments": [
            {"_id": "a1", "originalname": "data.docx", "type": "attachment"},
            {"_id": "a2", "originalname": "photo.jpeg", "type": "attachment"},
            {"_id": "a3", "originalname": "notes", "type": "attachment"},  # no extension
            {"_id": "a4", "originalname": "weird.zzz", "type": "attachment"},  # unknown ext
        ],
    }

    refs = extract_file_refs(raw)
    by_id = {r.file_id: r for r in refs}
    assert by_id["a1"].content_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert by_id["a2"].content_type == "image/jpeg"
    assert by_id["a3"].content_type == "application/octet-stream"
    assert by_id["a4"].content_type == "application/octet-stream"


def test_extract_file_refs_language_falls_back_to_none_when_entity_language_absent() -> None:
    raw = {"documents": [{"_id": "d1", "originalname": "r.pdf", "type": "document"}], "attachments": []}

    refs = extract_file_refs(raw)

    assert refs[0].language is None


def test_extract_file_refs_filename_falls_back_to_file_id_when_absent() -> None:
    raw = {
        "language": "en",
        "documents": [{"_id": "d1", "originalname": "r.pdf", "type": "document"}],  # no filename
        "attachments": [],
    }

    refs = extract_file_refs(raw)

    assert refs[0].filename == "d1"


# --- build_file_restore_actions --------------------------------------------


def test_build_file_restore_actions_orders_documents_then_attachments() -> None:
    refs = [
        FileRef(
            file_id="a1", kind="attachment", filename="ha", originalname="att.png", language="en", content_type="image/png"
        ),
        FileRef(
            file_id="d1",
            kind="document",
            filename="hd",
            originalname="doc.pdf",
            language="en",
            content_type="application/pdf",
        ),
        FileRef(
            file_id="a2", kind="attachment", filename="ha2", originalname="att2.png", language="en", content_type="image/png"
        ),
        FileRef(
            file_id="d2",
            kind="document",
            filename="hd2",
            originalname="doc2.pdf",
            language="en",
            content_type="application/pdf",
        ),
    ]

    actions = build_file_restore_actions(refs)

    assert [a.kind for a in actions] == ["upload_document", "upload_document", "upload_attachment", "upload_attachment"]
    assert [a.file_id for a in actions] == ["d1", "d2", "a1", "a2"]


def test_build_file_restore_actions_preserves_within_kind_order() -> None:
    refs = [
        FileRef(
            file_id="d2", kind="document", filename="h", originalname="b.pdf", language="en", content_type="application/pdf"
        ),
        FileRef(
            file_id="d1", kind="document", filename="h", originalname="a.pdf", language="en", content_type="application/pdf"
        ),
    ]

    actions = build_file_restore_actions(refs)

    assert [a.file_id for a in actions] == ["d2", "d1"]  # snapshot order preserved


def test_build_file_restore_actions_maps_fields() -> None:
    refs = [
        FileRef(
            file_id="d1",
            kind="document",
            filename="hd",
            originalname="doc.pdf",
            language="en",
            content_type="application/pdf",
        ),
        FileRef(
            file_id="a1", kind="attachment", filename="ha", originalname="att.png", language=None, content_type="image/png"
        ),
    ]

    actions = build_file_restore_actions(refs)

    doc, att = actions
    assert doc.kind == "upload_document"
    assert doc.file_id == "d1"
    assert doc.originalname == "doc.pdf"
    assert doc.language == "en"
    assert doc.content_type == "application/pdf"
    assert att.kind == "upload_attachment"
    assert att.language is None


def test_build_file_restore_actions_empty_input() -> None:
    assert build_file_restore_actions([]) == []


def test_file_restore_action_is_frozen() -> None:
    refs = [
        FileRef(
            file_id="d1", kind="document", filename="h", originalname="a.pdf", language="en", content_type="application/pdf"
        )
    ]
    action = build_file_restore_actions(refs)[0]

    with pytest.raises(Exception):
        action.kind = "upload_attachment"  # type: ignore[misc]


def test_file_ref_is_frozen() -> None:
    ref = FileRef(
        file_id="d1", kind="document", filename="h", originalname="a.pdf", language="en", content_type="application/pdf"
    )

    with pytest.raises(Exception):
        ref.file_id = "other"  # type: ignore[misc]
