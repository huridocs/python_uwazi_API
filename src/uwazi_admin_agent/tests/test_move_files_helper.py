"""Isolated tests for the ``move_files_to_entity`` bound helper (Part 3, merge support).

The real mover is I/O (it calls ports via ``loop.run_until_complete``), but it is
driven here against tiny REAL in-memory port classes (no mocks, no network - the
AGENTS.md-sanctioned pattern) so its decision/flow is verified offline: it
extracts uploaded-file refs, fetches bytes, re-uploads documents-then-attachments
to the target, counts moved/failed, and skips URL attachments. The scoped no-op
(dummy mode) and the unwired stub are also covered.
"""

import asyncio
from typing import Any, override

import pytest

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.use_cases.script_exec_namespace import (
    ScopeViolationError,
    _build_move_files_real_helper,
    _move_files_noop_scoped,
)

# --- in-memory ports (real classes, not mocks) ------------------------------


class InMemoryEntityRepo(EntityRepositoryPort):
    def __init__(self, raws: dict[str, dict[str, Any]]) -> None:
        self._raws = raws

    @override
    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        return self._raws[shared_id]

    @override
    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @override
    async def save_raw(self, raw: dict[str, Any]) -> None:
        raise NotImplementedError

    @override
    async def create_raw(self, raw: dict[str, Any]) -> str:
        raise NotImplementedError

    @override
    async def delete_by_shared_id(self, shared_id: str) -> None:
        raise NotImplementedError


class InMemoryFileRepo(FileRepositoryPort):
    def __init__(self, bytes_store: dict[str, bytes], *, fail_uploads: bool = False) -> None:
        self._bytes = bytes_store
        self._fail = fail_uploads
        self.uploads: list[tuple[str, str, str | None, str, str]] = []  # kind, to_sid, lang, title, content_type

    @override
    async def get_file_bytes(self, filename: str) -> bytes | None:
        return self._bytes.get(filename)

    @override
    async def upload_document(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("document", shared_id, language, title, content_type))
        return not self._fail

    @override
    async def upload_attachment(
        self, data: bytes, shared_id: str, language: str | None, title: str, content_type: str
    ) -> bool:
        self.uploads.append(("attachment", shared_id, language, title, content_type))
        return not self._fail


def _raw(shared_id: str, *, documents: list | None = None, attachments: list | None = None) -> dict[str, Any]:
    return {
        "_id": "o-" + shared_id,
        "sharedId": shared_id,
        "title": "T",
        "language": "en",
        "documents": documents or [],
        "attachments": attachments or [],
    }


def _repo(*raws: dict[str, Any]) -> InMemoryEntityRepo:
    """A repo over the given raws — the target's raw MUST be included (the mover
    fetches the target to build its duplicate-file index)."""
    return InMemoryEntityRepo({raw["sharedId"]: raw for raw in raws})


# --- real mover --------------------------------------------------------------


def test_moves_documents_then_attachments_and_skips_url_attachments() -> None:
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw(
            "S1",
            documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "hashd1", "size": 3}],
            attachments=[
                {"_id": "a1", "originalname": "b.txt", "filename": "hasha1", "size": 3},
                {"_id": "u1", "originalname": "link.html", "url": "http://x"},  # URL attachment - skipped
            ],
        )
        s2 = _raw("S2", attachments=[{"_id": "a2", "originalname": "c.png", "filename": "hasha2", "size": 3}])
        repo = _repo(s1, s2, _raw("T1"))
        file_repo = InMemoryFileRepo({"hashd1": b"PDF", "hasha1": b"TXT", "hasha2": b"PNG"})
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1", "S2"], "T1")

        assert result == {"moved": 3, "failed": 0, "skipped": 0}
        # Documents first, then attachments (extract_file_refs ordering).
        kinds = [u[0] for u in file_repo.uploads]
        assert kinds == ["document", "attachment", "attachment"]
        # All uploads target the new target sharedId.
        assert all(u[1] == "T1" for u in file_repo.uploads)
        # Original names preserved.
        titles = [u[3] for u in file_repo.uploads]
        assert titles == ["a.pdf", "b.txt", "c.png"]
        # Content types: document -> pdf; .txt -> text/plain; .png -> image/png.
        ctypes = [u[4] for u in file_repo.uploads]
        assert ctypes[0] == "application/pdf"
        assert ctypes[1] == "text/plain"
        assert ctypes[2] == "image/png"
    finally:
        loop.close()


def test_missing_bytes_counted_as_failed_not_raised() -> None:
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw(
            "S1",
            documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "gone"}],  # no bytes stored
        )
        repo = _repo(s1, _raw("T1"))
        file_repo = InMemoryFileRepo({})  # empty -> get_file_bytes returns None
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1"], "T1")

        assert result == {"moved": 0, "failed": 1, "skipped": 0}
        assert file_repo.uploads == []  # never reached upload
    finally:
        loop.close()


def test_failed_upload_counted_as_failed_not_raised() -> None:
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1", attachments=[{"_id": "a1", "originalname": "b.txt", "filename": "hasha1"}])
        repo = _repo(s1, _raw("T1"))
        file_repo = InMemoryFileRepo({"hasha1": b"TXT"}, fail_uploads=True)
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1"], "T1")

        assert result == {"moved": 0, "failed": 1, "skipped": 0}
        assert len(file_repo.uploads) == 1  # attempted, returned False
    finally:
        loop.close()


def test_source_with_no_files_is_a_noop() -> None:
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1")  # no documents/attachments
        repo = _repo(s1, _raw("T1"))
        file_repo = InMemoryFileRepo({})
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        assert move(["S1"], "T1") == {"moved": 0, "failed": 0, "skipped": 0}
        assert file_repo.uploads == []
    finally:
        loop.close()


def test_language_argument_overrides_default_when_provided() -> None:
    loop = asyncio.new_event_loop()
    try:
        # Source raw carries NO row language -> ref.language is None -> the caller's
        # `language` is the upload's locale (fallback). (When the source DOES carry a
        # language, the file keeps it - consistent with delete-revert's `_upload_one`.)
        s1 = {
            "_id": "o-S1",
            "sharedId": "S1",
            "title": "T",
            "documents": [{"_id": "d1", "originalname": "a.pdf", "filename": "h"}],
        }
        repo = _repo(s1, _raw("T1"))
        file_repo = InMemoryFileRepo({"h": b"PDF"})
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        move(["S1"], "T1", language="es")

        assert file_repo.uploads[0][2] == "es"  # upload language = es (caller fallback)
    finally:
        loop.close()


def test_source_row_language_takes_precedence_over_caller_language() -> None:
    loop = asyncio.new_event_loop()
    try:
        # Source row language is 'es' -> the file keeps it on move (matches
        # delete-revert `_upload_one` semantics); the caller's `language` is ignored.
        s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "h"}])
        s1["language"] = "es"
        repo = _repo(s1, _raw("T1"))
        file_repo = InMemoryFileRepo({"h": b"PDF"})
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        move(["S1"], "T1", language="en")

        assert file_repo.uploads[0][2] == "es"  # file keeps its source row language
    finally:
        loop.close()


# --- duplicate files are skipped, not re-uploaded ------------------------------


def test_byte_identical_file_on_two_sources_is_uploaded_once() -> None:
    """THE multiplication bug: duplicate entities carry byte-identical copies of
    the same file; re-uploading each copy left N copies on the target. The mover
    now uploads the first and SKIPS the rest (sha256-confirmed identical)."""
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "src1", "size": 3}])
        s2 = _raw("S2", documents=[{"_id": "d2", "originalname": "a.pdf", "filename": "src2", "size": 3}])
        repo = _repo(s1, s2, _raw("T1"))
        file_repo = InMemoryFileRepo({"src1": b"PDF", "src2": b"PDF"})  # byte-identical copies
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1", "S2"], "T1")

        assert result == {"moved": 1, "failed": 0, "skipped": 1}
        assert len(file_repo.uploads) == 1  # ONE copy landed on the target
    finally:
        loop.close()


def test_file_already_on_the_target_is_skipped() -> None:
    """The target's own copy wins: a source file byte-identical to one already on
    the target is skipped (the candidate is found by (kind, name, size), then
    byte-confirmed via its fetched bytes)."""
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "src1", "size": 3}])
        target = _raw("T1", documents=[{"_id": "t1", "originalname": "a.pdf", "filename": "tgt1", "size": 3}])
        repo = _repo(s1, target)
        file_repo = InMemoryFileRepo({"src1": b"PDF", "tgt1": b"PDF"})
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1"], "T1")

        assert result == {"moved": 0, "failed": 0, "skipped": 1}
        assert file_repo.uploads == []  # nothing re-uploaded
    finally:
        loop.close()


def test_same_name_and_size_but_different_bytes_still_uploads() -> None:
    """No-loss bias: a same-named, same-sized but DIFFERENT file is NOT a
    confirmed duplicate — it uploads (the skip key only nominates candidates;
    only a digest match skips)."""
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "src1", "size": 3}])
        target = _raw("T1", documents=[{"_id": "t1", "originalname": "a.pdf", "filename": "tgt1", "size": 3}])
        repo = _repo(s1, target)
        file_repo = InMemoryFileRepo({"src1": b"PDF", "tgt1": b"PDG"})  # same size, different bytes
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1"], "T1")

        assert result == {"moved": 1, "failed": 0, "skipped": 0}
        assert len(file_repo.uploads) == 1
    finally:
        loop.close()


def test_unfetchable_target_candidate_bytes_do_not_skip_the_upload() -> None:
    """A candidate whose bytes cannot be fetched is NOT a confirmed duplicate —
    the file uploads (a false skip would silently destroy it after the delete)."""
    loop = asyncio.new_event_loop()
    try:
        s1 = _raw("S1", documents=[{"_id": "d1", "originalname": "a.pdf", "filename": "src1", "size": 3}])
        target = _raw("T1", documents=[{"_id": "t1", "originalname": "a.pdf", "filename": "gone", "size": 3}])
        repo = _repo(s1, target)
        file_repo = InMemoryFileRepo({"src1": b"PDF"})  # target's copy unfetchable
        move = _build_move_files_real_helper(repo, file_repo, loop, "en")

        result = move(["S1"], "T1")

        assert result == {"moved": 1, "failed": 0, "skipped": 0}
        assert len(file_repo.uploads) == 1
    finally:
        loop.close()


# --- unwired stub (no ports) -------------------------------------------------


def test_unwired_helper_raises_when_called() -> None:
    loop = asyncio.new_event_loop()
    try:
        move = _build_move_files_real_helper(None, None, loop, "en")
        with pytest.raises(RuntimeError, match="requires a wired entity_repository"):
            move(["S1"], "T1")
    finally:
        loop.close()


# --- scoped no-op (dummy mode) -----------------------------------------------


def test_scoped_noop_returns_zero_in_scope() -> None:
    scope = {"D1", "D2", "T1"}
    move = _move_files_noop_scoped(scope)
    result = move(["D1", "D2"], "T1")
    assert result == {
        "moved": 0,
        "failed": 0,
        "skipped": 0,
        "note": "no-op in validation - dummies carry no uploaded files",
    }


def test_scoped_noop_refuses_out_of_scope() -> None:
    scope = {"D1", "T1"}
    move = _move_files_noop_scoped(scope)
    with pytest.raises(ScopeViolationError):
        move(["D1", "REAL-ID"], "T1")  # REAL-ID outside the dummy scope
    with pytest.raises(ScopeViolationError):
        move(["D1"], "REAL-TARGET")  # target outside scope
