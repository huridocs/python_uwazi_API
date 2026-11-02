"""Isolated unit tests for the pure merge-copy cleanup selection.

A merge re-uploads its sources' files onto the target, minting fresh file rows
that no entity snapshot can remove. :func:`merge_copy_candidates` decides which
live rows are worth byte-verifying; the digest confirmation itself lives in the
revert use case (I/O). These tests use literal raws and plain assertions: no
mocks, no network.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.file_restore import (
    extract_live_file_refs,
    merge_copy_candidates,
)
from uwazi_admin_agent.domain.snapshot import FileRef

_WINDOW_START = 1_700_000_000_000
_WINDOW_END = _WINDOW_START + 60_000


def _source_file(**overrides: object) -> FileRef:
    base: dict[str, object] = {
        "file_id": "s-doc",
        "kind": "document",
        "filename": "source-storage.pdf",
        "originalname": "source.pdf",
        "content_type": "application/pdf",
        "size": 12,
    }
    base.update(overrides)
    return FileRef(**base)


def _live_doc(file_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "_id": file_id,
        "filename": f"{file_id}.pdf",
        "originalname": "source.pdf",
        "size": 12,
        "creationDate": _WINDOW_START + 1_000,
    }
    base.update(overrides)
    return base


def _raw(*documents: dict[str, object], attachments: list[dict[str, object]] | None = None) -> dict[str, object]:
    return {"documents": list(documents), "attachments": list(attachments or [])}


def test_extract_live_file_refs_reads_ids_filenames_sizes_and_creation_dates() -> None:
    raw = _raw(
        _live_doc("d1", creationDate=_WINDOW_START),
        attachments=[
            {"_id": "a1", "filename": "a1.html", "originalname": "page.html", "size": 4, "creationDate": 7},
            {"_id": "url", "originalname": "remote", "url": "https://example.com/x"},
        ],
    )

    refs = extract_live_file_refs(raw)

    assert [(r.file_id, r.kind, r.creation_date_ms) for r in refs] == [
        ("d1", "document", _WINDOW_START),
        ("a1", "attachment", 7),
    ]


def test_a_file_created_inside_the_run_window_with_a_source_identity_is_a_candidate() -> None:
    raw = _raw(_live_doc("copied-doc"))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert [c.file_id for c in candidates] == ["copied-doc"]


def test_a_file_the_target_snapshot_recorded_before_the_run_is_never_a_candidate() -> None:
    raw = _raw(_live_doc("pre-existing"))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids={"pre-existing"},
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert candidates == []


def test_with_a_before_set_a_recorded_id_is_kept_even_without_a_creation_date() -> None:
    raw = _raw(_live_doc("new-doc", creationDate=None))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids={"old-doc"},
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert [c.file_id for c in candidates] == ["new-doc"]


def test_without_a_before_set_an_undated_file_is_kept() -> None:
    raw = _raw(_live_doc("undated", creationDate=None))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert candidates == []


def test_a_file_older_than_the_run_window_is_kept() -> None:
    raw = _raw(_live_doc("old", creationDate=_WINDOW_START - 86_400_000))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert candidates == []


def test_a_file_created_after_the_sources_were_deleted_is_kept() -> None:
    raw = _raw(_live_doc("later", creationDate=_WINDOW_END + 3_600_000))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert candidates == []


def test_a_different_size_or_kind_does_not_match_a_source_file() -> None:
    raw = _raw(
        _live_doc("wrong-size", size=99),
        attachments=[{"_id": "att", "filename": "att.pdf", "originalname": "source.pdf", "size": 12}],
    )

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file()],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    # The attachment shares the name/size but not the kind; the size differs on
    # the document. Neither is the merge's document copy.
    assert candidates == []


def test_an_unknown_source_size_only_loosens_the_identity_check() -> None:
    raw = _raw(_live_doc("copied-doc", size=999))

    candidates = merge_copy_candidates(
        raw,
        source_files=[_source_file(size=None)],
        before_file_ids=None,
        created_after_ms=_WINDOW_START,
        created_before_ms=_WINDOW_END,
    )

    assert [c.file_id for c in candidates] == ["copied-doc"]
