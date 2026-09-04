"""Isolated unit tests for the pure duplicate-file cleanup decision (domain/file_cleanup).

Per AGENTS.md: no mocks, no network, literal inputs only. Pins the no-loss
contract the cleanup helper relies on:

- a file is deleted ONLY when a byte-identical (same kind, same sha256) copy
  remains on the SAME entity — never the last copy, never across kinds;
- the keeper is the FIRST ref in extract_file_refs order (documents-then-
  attachments, then raw order);
- unfetchable bytes (digest ``None``) are identity-unconfirmed and never
  deleted;
- a copy a relationship connection cites is NEVER deleted (Uwazi tears down
  connections citing a deleted file) — it is kept and reported as
  ``kept_cited``;
- the relation ``file`` citations are read from the raw's ``relations``.
"""

from collections.abc import Mapping

from uwazi_admin_agent.domain.file_cleanup import cited_file_ids, plan_entity_cleanup
from uwazi_admin_agent.domain.file_dedupe import file_digest
from uwazi_admin_agent.domain.snapshot import FileRef


def _ref(file_id: str, originalname: str, *, kind: str = "document", filename: str | None = None) -> FileRef:
    """A minimal literal FileRef for the plan decision."""
    return FileRef(
        file_id=file_id,
        kind=kind,  # type: ignore[arg-type]
        filename=filename or "hash-" + file_id,
        originalname=originalname,
        language=None,
        content_type="application/pdf",
        size=None,
    )


def _digests(refs: list[FileRef], data: Mapping[str, bytes | None]) -> dict[str, str | None]:
    """file_id -> digest map, with ``None`` for the ids whose bytes are missing."""
    result: dict[str, str | None] = {}
    for ref in refs:
        raw = data.get(ref.file_id)
        result[ref.file_id] = file_digest(raw) if raw is not None else None
    return result


def test_cited_file_ids_reads_relation_file_citations() -> None:
    """A relation's ``file`` (a hex-string ObjectId in the API JSON) is a citation."""
    raw = {
        "relations": [
            {"entity": "E1", "file": "d1", "reference": {"text": "see p.3"}},
            {"entity": "E1", "file": "d2"},
            {"entity": "E2"},  # whole-entity connection: no file
        ]
    }
    assert cited_file_ids(raw) == {"d1", "d2"}


def test_cited_file_ids_tolerates_missing_relations_and_odd_shapes() -> None:
    """No relations / non-dict entries / extended-JSON $oid objects never raise."""
    assert cited_file_ids({}) == set()
    assert cited_file_ids({"relations": "not-a-list"}) == set()
    assert cited_file_ids({"relations": [None, 42, {"no": "file"}]}) == set()
    assert cited_file_ids({"relations": [{"file": {"$oid": "abc123"}}]}) == {"abc123"}
    assert cited_file_ids({"relations": [{"file": ""}, {"file": 17}]}) == set()


def test_no_duplicates_means_an_empty_plan() -> None:
    refs = [_ref("d1", "a.pdf"), _ref("d2", "b.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": b"B"}), set())
    assert plan.to_delete == []
    assert plan.kept_cited == []


def test_byte_identical_group_keeps_first_and_deletes_the_rest() -> None:
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf"), _ref("d3", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": b"A", "d3": b"A"}), set())
    assert [r.file_id for r in plan.to_delete] == ["d2", "d3"]
    assert plan.kept_cited == []


def test_keeper_survives_even_when_cited() -> None:
    """The group's first copy is the keeper whether or not it is cited — the
    citation only protects the copies that would otherwise be DELETED."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": b"A"}), {"d1"})
    assert [r.file_id for r in plan.to_delete] == ["d2"]
    assert plan.kept_cited == []


def test_cited_redundant_copy_is_kept_and_reported() -> None:
    """A connection citing the redundant copy protects it: kept, not deleted."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf"), _ref("d3", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": b"A", "d3": b"A"}), {"d2"})
    assert [r.file_id for r in plan.to_delete] == ["d3"]
    assert [r.file_id for r in plan.kept_cited] == ["d2"]


def test_all_redundant_members_cited_deletes_nothing() -> None:
    """Every would-be deletion protected by a citation -> the whole group stays
    (the no-loss bias: a leftover duplicate beats a torn-down reference)."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": b"A"}), {"d2"})
    assert plan.to_delete == []
    assert [r.file_id for r in plan.kept_cited] == ["d2"]


def test_same_name_different_bytes_is_not_a_duplicate() -> None:
    """The incident's real edge: a genuine translation shares the name but not
    the bytes — both copies are KEPT (identity is the digest, never the name)."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"ENGLISH", "d2": b"SPANISH"}), set())
    assert plan.to_delete == []
    assert plan.kept_cited == []


def test_unfetchable_bytes_are_never_deleted() -> None:
    """A file whose bytes cannot be fetched is identity-unconfirmed: it is
    never deleted, even when a same-named sibling exists."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "d2": None}), set())
    assert plan.to_delete == []
    assert plan.kept_cited == []


def test_documents_and_attachments_dedupe_separately() -> None:
    """The same bytes in the document slot vs. an attachment slot are different
    rows — deleting across kinds would reshape the entity, so no delete."""
    refs = [_ref("d1", "a.pdf", kind="document"), _ref("a1", "a.pdf", kind="attachment")]
    plan = plan_entity_cleanup(refs, _digests(refs, {"d1": b"A", "a1": b"A"}), set())
    assert plan.to_delete == []
    assert plan.kept_cited == []


def test_groups_dedupe_independently_in_raw_order() -> None:
    """Two duplicate document groups plus a duplicate attachment group each
    keep their first copy; the plan lists redundant members group by group in
    the group's first-member order (deterministic)."""
    refs = [
        _ref("d1", "a.pdf"),
        _ref("d2", "a.pdf"),
        _ref("d3", "b.pdf"),
        _ref("a1", "c.html", kind="attachment"),
        _ref("a2", "c.html", kind="attachment"),
        _ref("d4", "a.pdf"),
    ]
    data = {"d1": b"A", "d2": b"A", "d3": b"B", "a1": b"C", "a2": b"C", "d4": b"A"}
    plan = plan_entity_cleanup(refs, _digests(refs, data), set())
    assert [r.file_id for r in plan.to_delete] == ["d2", "d4", "a2"]
    assert plan.kept_cited == []


def test_missing_digest_entry_treated_as_unconfirmed() -> None:
    """A ref with no digest entry at all (caller skipped the fetch) is kept."""
    refs = [_ref("d1", "a.pdf"), _ref("d2", "a.pdf")]
    plan = plan_entity_cleanup(refs, {"d1": file_digest(b"A")}, set())
    assert plan.to_delete == []
    assert plan.kept_cited == []
