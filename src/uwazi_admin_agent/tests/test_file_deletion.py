"""Isolated unit tests for the pure explicit-file-deletion decisions.

Per AGENTS.md: no mocks/stubs, no network, no running Uwazi instance. Pure
functions with literal :class:`FileRef` inputs and plain assertions. What is
pinned:

- ``file_id`` is the precise form and WINS over a name on the same request;
- a name (+ optional ``kind``) resolving to exactly ONE ref deletes; ZERO
  matches is ``not_found``; MORE is ``ambiguous`` with the candidate ids
  (never a guess — the incident entity carried FOUR same-named documents);
- a connection-cited target is refused ``cited`` (the no-loss default);
- a duplicated request dict raises ``ValueError`` (double-delete race);
- two requests resolving to the same file raise ``ValueError``;
- a target whose bytes cannot be fetched is refused ``unavailable``;
- the ``DeletedFile`` record round-trips the metadata revert needs;
- the deleted-file verify check is a CONTAINMENT decision: missing content
  is a gap, extra copies are not (a dedupe revert re-creates duplicates),
  with unknown digests as wildcards (no false gaps).
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.deleted_file import DeletedFile, to_deleted_file, to_file_ref
from uwazi_admin_agent.domain.file_deletion import (
    assert_unique_deletion_requests,
    group_deletions_by_entity,
    refuse_unbackable_targets,
    resolve_deletion_requests,
)
from uwazi_admin_agent.domain.revert_verification import FileContentSignature, build_deleted_file_gaps
from uwazi_admin_agent.domain.snapshot import FileRef


def _ref(file_id: str, name: str, kind: str = "document", language: str | None = "en") -> FileRef:
    return FileRef(
        file_id=file_id,
        kind=kind,  # type: ignore[arg-type]
        filename=f"storage-{file_id}",
        originalname=name,
        language=language,
        content_type="application/pdf" if kind == "document" else "text/html",
        size=7,
    )


# --- request resolution ------------------------------------------------------


def test_file_id_resolves_precisely_and_wins_over_a_name() -> None:
    refs = [_ref("d1", "a.pdf"), _ref("d2", "b.pdf")]
    resolved = resolve_deletion_requests("E1", refs, set(), [{"shared_id": "E1", "file_id": "d2", "originalname": "a.pdf"}])
    assert [r.file_id for r in resolved.targets] == ["d2"]
    assert resolved.refusals == []


def test_name_with_unique_match_resolves() -> None:
    refs = [_ref("d1", "a.pdf"), _ref("d2", "b.pdf")]
    resolved = resolve_deletion_requests("E1", refs, set(), [{"shared_id": "E1", "originalname": "b.pdf"}])
    assert [r.file_id for r in resolved.targets] == ["d2"]


def test_kind_qualifies_a_name_match() -> None:
    refs = [_ref("d1", "a.pdf", kind="document"), _ref("h1", "a.pdf", kind="attachment")]
    resolved = resolve_deletion_requests(
        "E1", refs, set(), [{"shared_id": "E1", "originalname": "a.pdf", "kind": "attachment"}]
    )
    assert [r.file_id for r in resolved.targets] == ["h1"]


def test_ambiguous_name_is_refused_with_candidates_never_guessed() -> None:
    """The incident's shape: FOUR same-named document rows — a name request is
    refused with all candidate ids so the script re-issues with file_id."""
    refs = [_ref(f"d{i}", "a.pdf") for i in range(1, 5)]
    resolved = resolve_deletion_requests("E1", refs, set(), [{"shared_id": "E1", "originalname": "a.pdf"}])
    assert resolved.targets == []
    assert len(resolved.refusals) == 1
    refusal = resolved.refusals[0]
    assert refusal.reason == "ambiguous"
    assert refusal.matches == ["d1", "d2", "d3", "d4"]


def test_unknown_file_id_is_refused_not_found() -> None:
    refs = [_ref("d1", "a.pdf")]
    resolved = resolve_deletion_requests("E1", refs, set(), [{"shared_id": "E1", "file_id": "dX"}])
    assert resolved.targets == []
    assert resolved.refusals[0].reason == "not_found"


def test_unknown_name_is_refused_not_found() -> None:
    resolved = resolve_deletion_requests("E1", [_ref("d1", "a.pdf")], set(), [{"shared_id": "E1", "originalname": "zz.pdf"}])
    assert resolved.refusals[0].reason == "not_found"
    assert resolved.refusals[0].matches == []


def test_unnamed_request_is_refused_not_found() -> None:
    resolved = resolve_deletion_requests("E1", [_ref("d1", "a.pdf")], set(), [{"shared_id": "E1"}])
    assert resolved.refusals[0].reason == "not_found"


def test_cited_target_is_refused_cited() -> None:
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    resolved = resolve_deletion_requests("E1", refs, {"d2"}, [{"shared_id": "E1", "file_id": "d2"}])
    assert resolved.targets == []
    assert resolved.refusals[0].reason == "cited"
    assert resolved.refusals[0].file_id == "d2"


def test_two_requests_resolving_to_one_file_raise() -> None:
    refs = [_ref("d1", "a.pdf")]
    with pytest.raises(ValueError, match="resolve to the same"):
        resolve_deletion_requests(
            "E1", refs, set(), [{"shared_id": "E1", "file_id": "d1"}, {"shared_id": "E1", "originalname": "a.pdf"}]
        )


# --- the up-front duplicated-request guard ------------------------------------


def test_duplicated_request_dict_raises_value_error() -> None:
    deletions = [{"shared_id": "E1", "file_id": "d1"}, {"shared_id": "E1", "file_id": "d1"}]
    with pytest.raises(ValueError, match="at most once per call"):
        assert_unique_deletion_requests(deletions)


def test_distinct_requests_pass_the_guard() -> None:
    deletions = [{"shared_id": "E1", "file_id": "d1"}, {"shared_id": "E1", "file_id": "d2"}]
    assert_unique_deletion_requests(deletions)  # no raise


# --- byte fetchability (the backup precondition) ------------------------------


def test_unfetchable_target_is_refused_unavailable() -> None:
    resolved = resolve_deletion_requests(
        "E1",
        [_ref("d1", "a.pdf"), _ref("d2", "b.pdf")],
        set(),
        [{"shared_id": "E1", "file_id": "d1"}, {"shared_id": "E1", "file_id": "d2"}],
    )
    backed = refuse_unbackable_targets("E1", resolved, {"d1": b"A", "d2": None})
    assert [r.file_id for r in backed.targets] == ["d1"]
    assert [(r.file_id, r.reason) for r in backed.refusals] == [("d2", "unavailable")]


# --- grouping ------------------------------------------------------------------


def test_grouping_is_by_entity_in_first_appearance_order() -> None:
    deletions = [
        {"shared_id": "E1", "file_id": "d1"},
        {"shared_id": "E2", "file_id": "x1"},
        {"shared_id": "E1", "file_id": "d2"},
    ]
    grouped = group_deletions_by_entity(deletions)
    assert [(sid, len(reqs)) for sid, reqs in grouped] == [("E1", 2), ("E2", 1)]


def test_grouping_drops_nothing_it_raises_on_missing_shared_id() -> None:
    """A request without a shared_id is malformed and LOUD — silently dropping
    it would hide the loss of a deletion (and every mode validates through the
    same function, so the identical script fails identically everywhere)."""
    with pytest.raises(ValueError, match="must name a shared_id"):
        group_deletions_by_entity([{"file_id": "d1"}])


# --- the DeletedFile record ------------------------------------------------------


def test_to_deleted_file_carries_revert_metadata_and_source() -> None:
    record = to_deleted_file("E1", _ref("d1", "a.pdf"), "explicit")
    assert record == DeletedFile(
        shared_id="E1",
        file_id="d1",
        kind="document",
        originalname="a.pdf",
        filename="storage-d1",
        language="en",
        content_type="application/pdf",
        size=7,
        source="explicit",
    )


def test_to_file_ref_round_trips_for_the_verify_check() -> None:
    record = to_deleted_file("E1", _ref("h1", "doc.html", kind="attachment"), "dedupe")
    ref = to_file_ref(record)
    assert ref.file_id == "h1"
    assert ref.kind == "attachment"
    assert ref.originalname == "doc.html"
    assert ref.content_type == "text/html"


# --- the deleted-file verify check (containment) ----------------------------------


def sig(kind: str, name: str, digest: str | None) -> FileContentSignature:
    return (kind, name, digest)


def test_restored_content_matches_and_extras_are_not_gaps() -> None:
    """A dedupe revert RE-CREATES duplicates: the keeper plus 2 restored copies
    satisfy 2 expected signatures, and the extra keeper is NOT a gap."""
    expected = [sig("document", "a.pdf", "sha-a"), sig("document", "a.pdf", "sha-a")]
    actual = [sig("document", "a.pdf", "sha-a"), sig("document", "a.pdf", "sha-a"), sig("document", "a.pdf", "sha-a")]
    assert build_deleted_file_gaps("E1", expected, actual) == []


def test_missing_content_is_a_gap_with_the_expected_name() -> None:
    expected = [sig("document", "a.pdf", "sha-a")]
    assert [(g.gap, g.kind, g.originalname) for g in build_deleted_file_gaps("E1", expected, [])] == [
        ("missing", "document", "a.pdf")
    ]


def test_same_name_different_content_is_a_real_missing_gap() -> None:
    expected = [sig("document", "a.pdf", "sha-a")]
    actual = [sig("document", "a.pdf", "sha-B")]  # wrong bytes came back
    assert len(build_deleted_file_gaps("E1", expected, actual)) == 1


def test_unknown_digests_are_wildcards_never_false_gaps() -> None:
    """Bytes unavailable on either side degrade to a name match — an
    unverifiable digest must not fabricate a gap."""
    expected = [sig("document", "a.pdf", None)]
    actual = [sig("document", "a.pdf", "sha-a")]
    assert build_deleted_file_gaps("E1", expected, actual) == []
    expected_known = [sig("document", "a.pdf", "sha-a")]
    actual_unknown = [sig("document", "a.pdf", None)]
    assert build_deleted_file_gaps("E1", expected_known, actual_unknown) == []


def test_wildcard_actuals_are_not_double_consumed() -> None:
    expected = [sig("document", "a.pdf", "sha-a"), sig("document", "a.pdf", "sha-b")]
    actual = [sig("document", "a.pdf", None), sig("document", "b.pdf", "sha-x")]
    gaps = build_deleted_file_gaps("E1", expected, actual)
    assert len(gaps) == 1  # one wildcard satisfied one expectation; the other is missing


def test_kind_groups_separately() -> None:
    expected = [sig("attachment", "a.pdf", "sha-a")]
    actual = [sig("document", "a.pdf", "sha-a")]
    assert len(build_deleted_file_gaps("E1", expected, actual)) == 1
