"""Isolated unit tests for the pure revert-verification decision (Phase 6 DoD).

No mocks, no network — literal manifests + snapshots + current raws + plain
assertions.
"""

from datetime import datetime, timezone
from typing import Any

from uwazi_admin_agent.domain.manifest import EntityIdentity, MigrationManifest, RewiredRelationship, RunStatus
from uwazi_admin_agent.domain.revert_verification import (
    FileGap,
    RevertVerificationResult,
    VerificationMismatch,
    build_file_gaps,
    build_inbound_ref_gaps,
    build_relationship_gaps,
    format_verification_result,
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
    refs = [_file_ref("f1", "document", "report.pdf")]
    actual = {
        "documents": [{"originalname": "report.pdf"}],
        "attachments": [{"originalname": "a-link", "url": "https://example.com/x"}],
    }

    assert build_file_gaps("D", refs, actual) == []


def test_build_file_gaps_matches_by_originalname_and_kind_not_file_id() -> None:
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


# --- mutual-deleted relationships: metadata remap + direction-aware gaps ------


def _rel(entity: str, hub: str, template: str | None) -> dict[str, Any]:
    return {"entity": entity, "hub": hub, "template": template}


def _mutual_relations() -> list[dict[str, Any]]:
    # Two hubs: h1 = A->B (from A, to B); h2 = B->A (from B, to A). The
    # denormalized view shows both hubs from either endpoint.
    return [
        _rel("A", "h1", None),
        _rel("B", "h1", "rtype1"),
        _rel("B", "h2", None),
        _rel("A", "h2", "rtype1"),
    ]


def test_deleted_mutual_relationship_restored_verifies_ok() -> None:
    # A and B were mutually related (two hubs); both deleted and re-created
    # (newA, newB). The entity-save path rebuilt BOTH hubs from the remapped
    # metadata (no self-refs). After remapping the snapshot OLD refs, both sides
    # match and each direction-aware hub is present in the re-created relations.
    manifest = _manifest(
        deleted=[
            EntityIdentity(shared_id="A", restored_shared_id="newA", language="en"),
            EntityIdentity(shared_id="B", restored_shared_id="newB", language="en"),
        ]
    )
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A title",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "B", "label": "B title"}]},
                "relations": _mutual_relations(),
            },
        ),
        "B": _snapshot(
            "B",
            {
                "_id": "b1",
                "sharedId": "B",
                "title": "B title",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "A", "label": "A title"}]},
                "relations": _mutual_relations(),
            },
        ),
    }
    current = {
        "A": {
            "_id": "na1",
            "sharedId": "newA",
            "title": "A title",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "newB", "label": "B title"}]},
            "relations": [
                _rel("newA", "nh1", None),
                _rel("newB", "nh1", "rtype1"),
                _rel("newB", "nh2", None),
                _rel("newA", "nh2", "rtype1"),
            ],
        },
        "B": {
            "_id": "nb1",
            "sharedId": "newB",
            "title": "B title",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "newA", "label": "A title"}]},
            "relations": [
                _rel("newA", "nh1", None),
                _rel("newB", "nh1", "rtype1"),
                _rel("newB", "nh2", None),
                _rel("newA", "nh2", "rtype1"),
            ],
        },
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is True
    assert result.mismatches == []
    assert result.relationship_gaps == []


def test_deleted_mutual_relationship_not_restored_is_flagged() -> None:
    # The re-apply was skipped/failed: the re-created entities have NO hubs and
    # the metadata ref was stripped on create (not re-applied). The remap makes
    # the snapshot expect the NEW sharedId; the current has nothing -> entity
    # mismatch, plus a relationship_gap per missing direction (A'->B' and B'->A').
    manifest = _manifest(
        deleted=[
            EntityIdentity(shared_id="A", restored_shared_id="newA", language="en"),
            EntityIdentity(shared_id="B", restored_shared_id="newB", language="en"),
        ]
    )
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A title",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "B", "label": "B title"}]},
                "relations": _mutual_relations(),
            },
        ),
        "B": _snapshot(
            "B",
            {
                "_id": "b1",
                "sharedId": "B",
                "title": "B title",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "A", "label": "A title"}]},
                "relations": _mutual_relations(),
            },
        ),
    }
    current = {
        "A": {"_id": "na1", "sharedId": "newA", "title": "A title", "language": "en", "metadata": {}, "relations": []},
        "B": {"_id": "nb1", "sharedId": "newB", "title": "B title", "language": "en", "metadata": {}, "relations": []},
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is False
    assert any(m.kind == "entity" and m.shared_id == "A" for m in result.mismatches)
    assert len(result.relationship_gaps) == 2
    gap_pairs = {(g.from_shared_id, g.to_shared_id) for g in result.relationship_gaps}
    assert gap_pairs == {("newA", "newB"), ("newB", "newA")}
    assert all(g.relation_type == "rtype1" for g in result.relationship_gaps)


def test_deleted_mutual_one_direction_missing_is_flagged() -> None:
    # Only the A'->B' hub came back; the B'->A' hub is missing. A direction-
    # unaware check would false-pass (both share the {newA,newB} endpoint set);
    # the direction-aware check flags exactly the missing B'->A' direction.
    manifest = _manifest(
        deleted=[
            EntityIdentity(shared_id="A", restored_shared_id="newA", language="en"),
            EntityIdentity(shared_id="B", restored_shared_id="newB", language="en"),
        ]
    )
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                "relations": _mutual_relations(),
            },
        ),
        "B": _snapshot(
            "B",
            {
                "_id": "b1",
                "sharedId": "B",
                "title": "B",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "A", "label": "A"}]},
                "relations": _mutual_relations(),
            },
        ),
    }
    current = {
        "A": {
            "_id": "na1",
            "sharedId": "newA",
            "title": "A",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "newB", "label": "B"}]},
            "relations": [_rel("newA", "nh1", None), _rel("newB", "nh1", "rtype1")],
        },
        "B": {
            "_id": "nb1",
            "sharedId": "newB",
            "title": "B",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "newA", "label": "A"}]},
            "relations": [_rel("newA", "nh1", None), _rel("newB", "nh1", "rtype1")],
        },
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is False
    assert len(result.relationship_gaps) == 1
    assert result.relationship_gaps[0].from_shared_id == "newB"
    assert result.relationship_gaps[0].to_shared_id == "newA"


def test_deleted_ref_to_still_existing_entity_verifies_ok_without_remap() -> None:
    # A was deleted (re-created newA) and referenced STATE (still exists). The
    # STATE ref is preserved on create and not remapped (STATE not in id_map);
    # the one-way hub is auto-restored by the create path, so no relationship gap.
    manifest = _manifest(deleted=[EntityIdentity(shared_id="A", restored_shared_id="newA", language="en")])
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A title",
                "language": "en",
                "metadata": {"entity_relation": [{"value": "STATE", "label": "State"}]},
                "relations": [_rel("A", "h1", None), _rel("STATE", "h1", "rtype1")],
            },
        ),
    }
    current = {
        "A": {
            "_id": "na1",
            "sharedId": "newA",
            "title": "A title",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "STATE", "label": "State"}]},
            "relations": [_rel("newA", "nh", None), _rel("STATE", "nh", "rtype1")],
        },
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is True
    assert result.relationship_gaps == []


# --- build_relationship_gaps (direction-aware) -------------------------------


def _hub(from_sid: str, to_sid: str, hub: str, relation_type: str):
    from uwazi_admin_agent.domain.relationship_restore import CapturedHub

    return CapturedHub(hub=hub, from_shared_id=from_sid, to_shared_id=to_sid, relation_type=relation_type)


def test_build_relationship_gaps_missing_hub_is_flagged() -> None:
    hubs = [_hub("A", "B", "h1", "rtype1")]
    current = {"A": {"relations": []}}

    gaps = build_relationship_gaps(hubs, {"A": "newA", "B": "newB"}, current)

    assert len(gaps) == 1
    assert gaps[0].gap == "missing"
    assert gaps[0].from_shared_id == "newA"
    assert gaps[0].to_shared_id == "newB"


def test_build_relationship_gaps_present_hub_is_empty() -> None:
    hubs = [_hub("A", "B", "h1", "rtype1")]
    current = {"A": {"relations": [_rel("newA", "nh", None), _rel("newB", "nh", "rtype1")]}}

    assert build_relationship_gaps(hubs, {"A": "newA", "B": "newB"}, current) == []


def test_build_relationship_gaps_reversed_direction_is_flagged() -> None:
    # The hub present is B'->A' (from=newB), but we expect A'->B' (from=newA).
    # Direction-awareness flags it; an endpoint-set check would false-pass.
    hubs = [_hub("A", "B", "h1", "rtype1")]
    current = {"A": {"relations": [_rel("newB", "nh", None), _rel("newA", "nh", "rtype1")]}}

    gaps = build_relationship_gaps(hubs, {"A": "newA", "B": "newB"}, current)

    assert len(gaps) == 1
    assert gaps[0].from_shared_id == "newA"


def test_build_relationship_gaps_skips_unmapped_endpoint() -> None:
    hubs = [_hub("A", "B", "h1", "rtype1")]
    assert build_relationship_gaps(hubs, {"A": "newA"}, {"A": {"relations": []}}) == []


def test_build_relationship_gaps_skips_none_current() -> None:
    hubs = [_hub("A", "B", "h1", "rtype1")]
    assert build_relationship_gaps(hubs, {"A": "newA", "B": "newB"}, {"A": None}) == []


# --- build_inbound_ref_gaps (still-existing -> re-created) --------------------


def _inbound(existing: str, deleted: str, relation_type: str):
    from uwazi_admin_agent.domain.relationship_restore import InboundRef

    return InboundRef(existing_shared_id=existing, deleted_shared_id=deleted, relation_type=relation_type)


def test_build_inbound_ref_gaps_missing_hub_is_flagged() -> None:
    refs = [_inbound("B", "A", "rtype1")]
    current = {"B": {"relations": []}}

    gaps = build_inbound_ref_gaps(refs, {"A": "newA"}, current)

    assert len(gaps) == 1
    assert gaps[0].gap == "missing"
    assert gaps[0].shared_id == "B"
    assert gaps[0].from_shared_id == "B"
    assert gaps[0].to_shared_id == "newA"
    assert gaps[0].relation_type == "rtype1"


def test_build_inbound_ref_gaps_present_hub_is_empty() -> None:
    refs = [_inbound("B", "A", "rtype1")]
    current = {"B": {"relations": [_rel("B", "nh", None), _rel("newA", "nh", "rtype1")]}}

    assert build_inbound_ref_gaps(refs, {"A": "newA"}, current) == []


def test_build_inbound_ref_gaps_skips_unmapped_deleted() -> None:
    # A was not re-created (not in id_map) — the entity mismatch flags that.
    refs = [_inbound("B", "A", "rtype1")]
    assert build_inbound_ref_gaps(refs, {}, {"B": {"relations": []}}) == []


def test_build_inbound_ref_gaps_skips_none_current() -> None:
    refs = [_inbound("B", "A", "rtype1")]
    assert build_inbound_ref_gaps(refs, {"A": "newA"}, {"B": None}) == []


def test_verify_revert_inbound_ref_restored_verifies_ok() -> None:
    # Defect 1: A deleted (re-created newA); B still-existing had B->A (cascade-
    # stripped). Revert re-added B->newA. B is NOT in the manifest; verify
    # discovers it from A's snapshot relations and checks B's relations.
    manifest = _manifest(deleted=[EntityIdentity(shared_id="A", restored_shared_id="newA", language="en")])
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A",
                "language": "en",
                "template": "tmplA",
                "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                "relations": [
                    _rel("A", "h2", None),
                    _rel("B", "h2", "rtype1"),
                    _rel("B", "h1", None),
                    _rel("A", "h1", "rtype1"),
                ],
            },
        ),
    }
    current = {
        "A": {
            "_id": "na1",
            "sharedId": "newA",
            "title": "A",
            "language": "en",
            "template": "tmplA",
            "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
            "relations": [_rel("newA", "h2", None), _rel("B", "h2", "rtype1")],
        },
        "B": {
            "_id": "b1",
            "sharedId": "B",
            "title": "B",
            "language": "en",
            "metadata": {"entity_relation": [{"value": "newA", "label": "A"}]},
            "relations": [_rel("B", "h1", None), _rel("newA", "h1", "rtype1")],
        },
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is True
    assert result.relationship_gaps == []


def test_verify_revert_inbound_ref_not_restored_is_flagged() -> None:
    # B->A was cascade-stripped and revert did NOT re-add it: B has no hub to
    # newA. The inbound gap check flags B (a non-manifest entity).
    manifest = _manifest(deleted=[EntityIdentity(shared_id="A", restored_shared_id="newA", language="en")])
    snapshots = {
        "A": _snapshot(
            "A",
            {
                "_id": "a1",
                "sharedId": "A",
                "title": "A",
                "language": "en",
                "template": "tmplA",
                "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
                "relations": [_rel("B", "h1", None), _rel("A", "h1", "rtype1")],
            },
        ),
    }
    current = {
        "A": {
            "_id": "na1",
            "sharedId": "newA",
            "title": "A",
            "language": "en",
            "template": "tmplA",
            "metadata": {"entity_relation": [{"value": "B", "label": "B"}]},
            "relations": [_rel("newA", "h2", None), _rel("B", "h2", "rtype1")],
        },
        "B": {"_id": "b1", "sharedId": "B", "title": "B", "language": "en", "relations": []},
    }

    result = verify_revert(manifest, snapshots, current)

    assert result.ok is False
    assert len(result.relationship_gaps) == 1
    assert result.relationship_gaps[0].shared_id == "B"
    assert result.relationship_gaps[0].to_shared_id == "newA"


def test_relationship_gap_is_frozen() -> None:
    import pytest

    from uwazi_admin_agent.domain.revert_verification import RelationshipGap

    gap = RelationshipGap(shared_id="A", from_shared_id="newA", to_shared_id="newB", relation_type="r")
    with pytest.raises(Exception):
        gap.gap = "extra"  # type: ignore[misc]


# --- format_verification_result ----------------------------------------------


def test_format_verification_result_ok() -> None:
    result = RevertVerificationResult(ok=True, checked=3, mismatches=[], file_gaps=[])
    text = format_verification_result(result)
    assert "checked=3" in text
    assert "0 mismatch" in text


def test_format_verification_result_lists_mismatches() -> None:
    result = RevertVerificationResult(
        ok=False,
        checked=2,
        mismatches=[
            VerificationMismatch(
                shared_id="A",
                kind="entity",
                expected={"title": "old"},
                actual=None,
            ),
            VerificationMismatch(
                shared_id="C",
                kind="created",
                expected=None,
                actual={"title": "survived"},
            ),
        ],
    )
    text = format_verification_result(result)
    lines = text.splitlines()
    assert len(lines) == 3
    assert "entity A" in lines[1]
    assert "expected" in lines[1]
    assert "got None" in lines[1]
    assert "created C" in lines[2]


def test_format_verification_result_lists_file_gaps() -> None:
    result = RevertVerificationResult(
        ok=False,
        checked=1,
        file_gaps=[FileGap(shared_id="D", gap="missing", originalname="report.pdf", kind="document")],
    )
    text = format_verification_result(result)
    assert "file missing document 'report.pdf' on D" in text


def test_format_verification_result_truncates_long_values() -> None:
    long_title = "x" * 200
    result = RevertVerificationResult(
        ok=False,
        checked=1,
        mismatches=[
            VerificationMismatch(shared_id="A", kind="entity", expected=long_title, actual="short"),
        ],
    )
    text = format_verification_result(result)
    assert "xxx..." in text
    assert long_title not in text
