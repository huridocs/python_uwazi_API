"""Isolated unit tests for the pure duplicate-file identity (domain/file_dedupe).

Per AGENTS.md: no mocks, no network, literal inputs only. Pins the decision
pieces the merge file-move relies on so merging N duplicate entities leaves
ONE copy of each unique file instead of multiplying it (the live incident):
- the cheap candidate key (kind, originalname, size) nominates ONLY same-key
  target files — never decides a skip by itself;
- a skip is decided solely by a sha256 digest match;
- ``extract_file_refs`` surfaces the raw entry's ``size`` (the key's third leg).
"""

import hashlib

from uwazi_admin_agent.domain.file_dedupe import TargetFileDeduper, file_digest, file_identity_key
from uwazi_admin_agent.domain.file_restore import extract_file_refs
from uwazi_admin_agent.domain.snapshot import FileRef


def _ref(originalname: str, size: int | None, *, kind: str = "document", filename: str | None = None) -> FileRef:
    return FileRef(
        file_id="f-" + originalname,
        kind=kind,  # type: ignore[arg-type]
        filename=filename or "hash-" + originalname,
        originalname=originalname,
        language=None,
        content_type="application/pdf",
        size=size,
    )


def test_identity_key_is_kind_name_and_size() -> None:
    """The cheap key: (kind, originalname, size) — each leg separates identities."""
    assert file_identity_key(_ref("a.pdf", 3)) == ("document", "a.pdf", 3)
    assert file_identity_key(_ref("a.pdf", 4)) != file_identity_key(_ref("a.pdf", 3))
    assert file_identity_key(_ref("a.pdf", 3, kind="attachment")) != file_identity_key(_ref("a.pdf", 3))
    assert file_identity_key(_ref("b.pdf", 3)) != file_identity_key(_ref("a.pdf", 3))


def test_digest_is_sha256_hex_of_the_bytes() -> None:
    """The exact byte identity — same bytes, same digest; different bytes, different."""
    assert file_digest(b"PDF") == hashlib.sha256(b"PDF").hexdigest()
    assert file_digest(b"PDF") == file_digest(b"PDF")
    assert file_digest(b"PDF") != file_digest(b"PDf")


def test_candidates_nominate_only_same_key_target_files() -> None:
    """A novel key has no candidates; a matching key nominates every same-key file."""
    deduper = TargetFileDeduper([_ref("a.pdf", 3), _ref("a.pdf", 3, filename="other"), _ref("a.pdf", 4)])
    assert deduper.candidates(_ref("a.pdf", 3)) == [_ref("a.pdf", 3), _ref("a.pdf", 3, filename="other")]
    assert deduper.candidates(_ref("a.pdf", 4)) == [_ref("a.pdf", 4)]
    assert deduper.candidates(_ref("zzz.pdf", 9)) == []
    # A same-name different-size file is NOT a candidate (it is a different file).
    assert deduper.candidates(_ref("a.pdf", 5)) == []


def test_remembered_digests_are_reported_present() -> None:
    """has_digest is False until remember records the digest (upload or byte-confirm)."""
    deduper = TargetFileDeduper([])
    digest = file_digest(b"PDF")
    assert deduper.has_digest(digest) is False
    deduper.remember(digest)
    assert deduper.has_digest(digest) is True


def test_extract_file_refs_carries_the_raw_entry_size() -> None:
    """The raw's file entries carry ``size``; FileRef surfaces it (None when absent)."""
    raw = {
        "_id": "o-S1",
        "sharedId": "S1",
        "language": "en",
        "documents": [{"_id": "d1", "originalname": "a.pdf", "filename": "hashd1", "size": 82739}],
        "attachments": [{"_id": "a1", "originalname": "b.html", "filename": "hasha1", "size": 2048}],
    }
    refs = extract_file_refs(raw)
    assert [r.size for r in refs] == [82739, 2048]

    no_size = {
        "_id": "o-S2",
        "sharedId": "S2",
        "documents": [{"_id": "d2", "originalname": "c.pdf", "filename": "hashd2"}],
    }
    assert extract_file_refs(no_size)[0].size is None
