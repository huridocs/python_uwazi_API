"""Isolated unit tests for the pure revert-verification decision (Phase 6 DoD).

No mocks, no network — literal manifests + snapshots + current raws + plain
assertions.
"""

from datetime import datetime, timezone
from typing import Any

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.revert_verification import (
    RevertVerificationResult,
    VerificationMismatch,
    build_file_gaps,
    verify_revert,
)
from uwazi_admin_agent.domain.snapshot import EntitySnapshot, FileRef


def _snapshot(shared_id: str, raw: dict[str, Any]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _snapshot_with_files(shared_id: str, raw: dict[str, Any], files: list[FileRef]) -> EntitySnapshot:
    return EntitySnapshot(
        shared_id=shared_id,
        internal_id=raw.get("_id"),
        language=raw.get("language"),
        raw=raw,
        captured_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        files=files,
    )


def _file_ref(file_id: str, kind: str, originalname: str, language: str | None = "en") -> FileRef:
    return FileRef(
        file_id=file_id,
        kind=kind,  # type: ignore[arg-type]
        filename=f"h-{file_id}",
        originalname=originalname,
        language=language,
        content_type="application/pdf" if kind == "document" else "image/png",
    )


def _manifest(
    modified: list[EntityIdentity] | None = None,
    deleted: list[EntityIdentity] | None = None,
    created: list[EntityIdentity] | None = None,
    rewired: list[RewiredRelationship] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        modified=modified or [],
        deleted=deleted or [],
        created=created or [],
        rewired=rewired or [],
        status=RunStatus.REVERTED,
    )


# --- modified entities -----------------------------------------------------


def test_modified_entity_matching_snapshot_is_ok() -> None:
    manifest = _manifest(modified=[EntityIdentity(shared_id="A")])
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "language": "en"})}

    result = verify_revert(manifest, snapshots, {"A": {"_id": "a1", "title": "old", "language": "en"}})

    assert result.ok is True
    assert result.checked == 1
    assert result.mismatches == []


def test_modified_entity_data_field_mismatch_is_flagged() -> None:
    manifest = _manifest(modified=[EntityIdentity(shared_id="A")])
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "language": "en"})}

    result = verify_revert(manifest, snapshots, {"A": {"_id": "a1", "title": "WRONG", "language": "en"}})

    assert result.ok is False
    assert len(result.mismatches) == 1
    assert result.mismatches[0].kind == "entity"
    assert result.mismatches[0].shared_id == "A"


def test_modified_entity_editdate_only_difference_is_ok() -> None:
    # editDate is platform-managed — a difference there alone is not a mismatch.
    manifest = _manifest(modified=[EntityIdentity(shared_id="A")])
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "editDate": 1000, "language": "en"})}

    result = verify_revert(manifest, snapshots, {"A": {"_id": "a1", "title": "old", "editDate": 1005, "language": "en"}})

    assert result.ok is True
    assert result.mismatches == []


def test_modified_entity_absent_after_revert_is_flagged() -> None:
    manifest = _manifest(modified=[EntityIdentity(shared_id="A")])
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "language": "en"})}

    result = verify_revert(manifest, snapshots, {"A": None})

    assert result.ok is False
    assert result.mismatches[0].kind == "entity"
    assert result.mismatches[0].actual is None


# --- deleted entities (re-created from snapshot) ---------------------------


def test_deleted_entity_recreated_matching_snapshot_is_ok() -> None:
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {"D": _snapshot("D", {"_id": "d1", "title": "old D", "language": "en"})}

    result = verify_revert(manifest, snapshots, {"D": {"_id": "d1", "title": "old D", "language": "en"}})

    assert result.ok is True
    assert result.checked == 1


def test_deleted_entity_not_recreated_is_flagged() -> None:
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {"D": _snapshot("D", {"_id": "d1", "title": "old D", "language": "en"})}

    result = verify_revert(manifest, snapshots, {"D": None})

    assert result.ok is False
    assert result.mismatches[0].kind == "entity"
    assert result.mismatches[0].shared_id == "D"


# --- created entities (revert deleted them) --------------------------------


def test_created_entity_gone_after_revert_is_ok() -> None:
    manifest = _manifest(created=[EntityIdentity(shared_id="C")])

    result = verify_revert(manifest, snapshots={}, current_raws={"C": None})

    assert result.ok is True
    assert result.checked == 1


def test_created_entity_still_present_is_flagged() -> None:
    manifest = _manifest(created=[EntityIdentity(shared_id="C")])

    result = verify_revert(manifest, snapshots={}, current_raws={"C": {"_id": "c1", "title": "survived", "language": "en"}})

    assert result.ok is False
    assert result.mismatches[0].kind == "created"
    assert result.mismatches[0].expected is None
    assert result.mismatches[0].actual == {"_id": "c1", "title": "survived", "language": "en"}


# --- rewired relationships -------------------------------------------------


def test_rewired_relationship_matching_before_is_ok() -> None:
    before = [{"_id": "r1", "label": "old"}]
    manifest = _manifest(
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(shared_id="R", language="en"),
                property_name="relations",
                before=before,
            )
        ],
    )

    result = verify_revert(manifest, snapshots={}, current_raws={"R": {"_id": "r1", "relations": before, "language": "en"}})

    assert result.ok is True


def test_rewired_relationship_mismatch_is_flagged() -> None:
    before = [{"_id": "r1", "label": "old"}]
    manifest = _manifest(
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(shared_id="R", language="en"),
                property_name="relations",
                before=before,
            )
        ],
    )

    result = verify_revert(
        manifest,
        snapshots={},
        current_raws={"R": {"_id": "r1", "relations": [{"_id": "r2", "label": "WRONG"}], "language": "en"}},
    )

    assert result.ok is False
    assert result.mismatches[0].kind == "relationship"
    assert result.mismatches[0].shared_id == "R"
    assert result.mismatches[0].expected == before
    assert result.mismatches[0].actual == [{"_id": "r2", "label": "WRONG"}]


def test_rewired_when_current_raw_absent_is_flagged() -> None:
    manifest = _manifest(
        rewired=[
            RewiredRelationship(
                entity=EntityIdentity(shared_id="R", language="en"),
                property_name="relations",
                before=[],
            )
        ],
    )

    result = verify_revert(manifest, snapshots={}, current_raws={"R": None})

    assert result.ok is False
    assert result.mismatches[0].kind == "relationship"
    assert result.mismatches[0].actual is None


# --- combined + empty -----------------------------------------------------


def test_empty_manifest_is_ok_with_zero_checks() -> None:
    result = verify_revert(_manifest(), snapshots={}, current_raws={})

    assert result.ok is True
    assert result.checked == 0
    assert result.mismatches == []


def test_combined_modified_and_created_counts_all() -> None:
    manifest = _manifest(
        modified=[EntityIdentity(shared_id="A")],
        created=[EntityIdentity(shared_id="C")],
    )
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "language": "en"})}

    result = verify_revert(
        manifest,
        snapshots,
        current_raws={
            "A": {"_id": "a1", "title": "old", "language": "en"},
            "C": None,
        },
    )

    assert result.ok is True
    assert result.checked == 2  # modified + created


def test_combined_reports_multiple_mismatches() -> None:
    manifest = _manifest(
        modified=[EntityIdentity(shared_id="A")],
        created=[EntityIdentity(shared_id="C")],
    )
    snapshots = {"A": _snapshot("A", {"_id": "a1", "title": "old", "language": "en"})}

    result = verify_revert(
        manifest,
        snapshots,
        current_raws={
            "A": {"_id": "a1", "title": "WRONG", "language": "en"},
            "C": {"_id": "c1", "title": "survived", "language": "en"},
        },
    )

    assert result.ok is False
    assert {m.shared_id for m in result.mismatches} == {"A", "C"}
    kinds = {m.shared_id: m.kind for m in result.mismatches}
    assert kinds == {"A": "entity", "C": "created"}


# --- result models ---------------------------------------------------------


def test_verification_mismatch_is_frozen() -> None:
    import pytest

    m = VerificationMismatch(shared_id="A", kind="entity", expected={}, actual=None)
    with pytest.raises(Exception):
        m.kind = "created"  # type: ignore[misc]


def test_revert_verification_result_constructs() -> None:
    result = RevertVerificationResult(ok=True, checked=0, mismatches=[])
    assert result.ok is True
    assert result.checked == 0


# --- deleted entities: documents/attachments excluded from raw-diff -----------


def test_deleted_entity_reuploaded_files_do_not_trigger_raw_mismatch() -> None:
    # The snapshot raw carries the original documents/attachments arrays (with
    # original file _ids/filenames). The re-created entity has re-uploaded files
    # with fresh _ids/filenames but the same originalnames — the raw-diff must
    # exclude documents/attachments (file identity is re-minted) and the
    # file-gap check must pass (originalnames match).
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {
        "D": _snapshot_with_files(
            "D",
            {
                "_id": "d1",
                "sharedId": "D",
                "title": "old D",
                "language": "en",
                "documents": [{"_id": "fold", "originalname": "report.pdf", "filename": "oldhash", "language": "eng"}],
                "attachments": [],
            },
            files=[_file_ref("fold", "document", "report.pdf")],
        )
    }
    current = {
        "D": {
            "_id": "new1",
            "sharedId": "NEW",
            "title": "old D",
            "language": "en",
            "documents": [{"_id": "fnew", "originalname": "report.pdf", "filename": "newhash", "language": "eng"}],
            "attachments": [],
        }
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is True
    assert result.mismatches == []
    assert result.file_gaps == []


# --- file-gap check (build_file_gaps + verify_revert wiring) ------------------


def test_build_file_gaps_missing_document_is_flagged() -> None:
    refs = [_file_ref("f1", "document", "report.pdf"), _file_ref("f2", "attachment", "scan.png")]
    actual = {"documents": [{"originalname": "report.pdf", "language": "eng"}], "attachments": []}

    gaps = build_file_gaps("D", refs, actual)

    assert len(gaps) == 1
    assert gaps[0].gap == "missing"
    assert gaps[0].originalname == "scan.png"
    assert gaps[0].kind == "attachment"


def test_build_file_gaps_extra_file_is_flagged() -> None:
    refs = [_file_ref("f1", "document", "report.pdf")]
    actual = {
        "documents": [{"originalname": "report.pdf"}],
        "attachments": [{"originalname": "unexpected.png"}],
    }

    gaps = build_file_gaps("D", refs, actual)

    assert len(gaps) == 1
    assert gaps[0].gap == "extra"
    assert gaps[0].originalname == "unexpected.png"
    assert gaps[0].kind == "attachment"


def test_build_file_gaps_all_present_is_empty() -> None:
    refs = [_file_ref("f1", "document", "report.pdf"), _file_ref("f2", "attachment", "scan.png")]
    actual = {
        "documents": [{"originalname": "report.pdf", "language": "eng"}],
        "attachments": [{"originalname": "scan.png"}],
    }

    assert build_file_gaps("D", refs, actual) == []


def test_build_file_gaps_ignores_url_attachments_in_actual() -> None:
    # URL attachments on the re-created entity are restored by the create path,
    # not by re-upload — they must not appear as "extra" gaps.
    refs = [_file_ref("f1", "document", "report.pdf")]
    actual = {
        "documents": [{"originalname": "report.pdf"}],
        "attachments": [{"originalname": "a-link", "url": "https://example.com/x"}],
    }

    assert build_file_gaps("D", refs, actual) == []


def test_build_file_gaps_matches_by_originalname_and_kind_not_file_id() -> None:
    # Fresh file _ids/filenames on re-upload must not cause gaps — the match is
    # by originalname + kind.
    refs = [_file_ref("old-id", "document", "report.pdf")]
    actual = {"documents": [{"_id": "fresh-id", "originalname": "report.pdf", "filename": "freshhash"}], "attachments": []}

    assert build_file_gaps("D", refs, actual) == []


def test_verify_revert_reports_file_gaps_for_deleted_with_files() -> None:
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {
        "D": _snapshot_with_files(
            "D",
            {"_id": "d1", "sharedId": "D", "title": "old D", "language": "en"},
            files=[_file_ref("f1", "document", "report.pdf"), _file_ref("f2", "attachment", "scan.png")],
        )
    }
    # Only the document came back; the attachment is missing.
    current = {
        "D": {
            "_id": "new1",
            "sharedId": "NEW",
            "title": "old D",
            "language": "en",
            "documents": [{"originalname": "report.pdf"}],
            "attachments": [],
        }
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is False
    assert len(result.file_gaps) == 1
    assert result.file_gaps[0].gap == "missing"
    assert result.file_gaps[0].originalname == "scan.png"


def test_verify_revert_skips_file_gap_check_when_actual_is_none() -> None:
    # Re-create failed (actual is None) — the entity mismatch already flags it;
    # the file-gap check must not crash on a None actual.
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {
        "D": _snapshot_with_files(
            "D",
            {"_id": "d1", "sharedId": "D", "title": "old D", "language": "en"},
            files=[_file_ref("f1", "document", "report.pdf")],
        )
    }

    result = verify_revert(manifest, snapshots, {"D": None})

    assert result.ok is False
    assert result.file_gaps == []
    assert result.mismatches[0].kind == "entity"


def test_verify_revert_skips_file_gap_check_when_snapshot_has_no_files() -> None:
    # An older snapshot (pre-change) has files=None — no file-gap check runs.
    manifest = _manifest(deleted=[EntityIdentity(shared_id="D")])
    snapshots = {"D": _snapshot("D", {"_id": "d1", "sharedId": "D", "title": "old D", "language": "en"})}
    current = {"D": {"_id": "new1", "sharedId": "NEW", "title": "old D", "language": "en"}}

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is True
    assert result.file_gaps == []


def test_file_gap_is_frozen() -> None:
    import pytest

    from uwazi_admin_agent.domain.revert_verification import FileGap

    gap = FileGap(shared_id="D", gap="missing", originalname="x.pdf", kind="document")
    with pytest.raises(Exception):
        gap.gap = "extra"  # type: ignore[misc]
