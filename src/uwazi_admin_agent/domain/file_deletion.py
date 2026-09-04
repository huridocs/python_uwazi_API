"""Pure target-resolution decisions for EXPLICIT file deletions.

The general file-deletion nominator ("delete supporting file X from entity
Y") differs from the dedupe nominator in one way: the operator NAMES the
files, so the decision layer must translate names into file rows — and
REFUSE rather than guess whenever the naming is ambiguous or the target is
unsafe. The no-loss rules are the same bias as :mod:`domain.file_cleanup`:

- a file one of the entity's own relationship connections cites is NEVER
  deleted (Uwazi's file delete tears the citing connection down with the
  file — ``FilesService.delete`` → ``relV1DS.deleteByFiles``). v1 has no
  force flag: the refusal is reported, and the operator can rewire or delete
  it by hand;
- ``file_id`` is the precise form (discovered via ``get_entity_files``):
  when it is present it wins and any ``originalname`` on the same request is
  ignored;
- ``originalname`` (+ optional ``kind``) is a convenience resolver: exactly
  ONE match resolves; ZERO matches is refused ``not_found``; MORE than one
  match is refused ``ambiguous`` with the candidate file ids in the report —
  never a guess (the incident entity carried FOUR same-named document rows,
  1 English + 3 Spanish, so scripts must name files explicitly);
- a target whose bytes cannot be fetched is refused ``unavailable``: the
  deletion core backs up bytes BEFORE the delete call (the revert
  precondition), and a file it cannot back up is a file it will not delete;
- URL attachments are never resolvable (they have no stored bytes to back
  up), so a request naming one resolves to ``not_found``.

The unique-target guard mirrors :func:`assert_unique_shared_ids`: a request
dict appearing twice, or two requests resolving to the same file row, would
race one delete call against an already-deleted row (the loser's ``False``
is miscounted as ``failed``). Duplicated requests are refused up front —
loudly, in every namespace, before any I/O.

Pure: no I/O — the caller fetches the raw, extracts the refs, computes the
cited ids, and hands them in.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.snapshot import FileRef

RefusalReason = Literal["not_found", "ambiguous", "cited", "unavailable"]


class DeletionRefusal(BaseModel):
    """One deletion request the nominator refused, with the reason why.

    ``matches`` carries the candidate ``file_id``s on an ``ambiguous``
    name-based request so the report tells the operator (or the script) which
    explicit ids to re-issue the deletion with.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The entity the request targeted.")
    file_id: str | None = Field(default=None, description="The requested file _id, when the request carried one.")
    originalname: str | None = Field(default=None, description="The requested name, when the request named the file.")
    kind: str | None = Field(default=None, description="The requested kind, when the request carried one.")
    reason: RefusalReason = Field(description="Why the request was refused.")
    matches: list[str] = Field(default_factory=list, description="Candidate file ids for an ambiguous name-based request.")


class ResolvedDeletions(BaseModel):
    """What one entity's explicit deletion requests resolve to (pure decision output).

    ``targets`` are the unambiguous, uncited, existing files to delete (in
    request order); ``refusals`` are the refused requests with their reasons.
    Byte fetchability is decided by the flow layer (it needs I/O) and turns a
    target into an ``unavailable`` refusal.
    """

    model_config = ConfigDict(frozen=True)

    targets: list[FileRef] = Field(default_factory=list, description="Files to delete, in request order.")
    refusals: list[DeletionRefusal] = Field(default_factory=list, description="Refused requests with reasons.")


def assert_unique_deletion_requests(deletions: list[dict[str, Any]]) -> None:
    """Refuse a request dict appearing twice inside ONE call (double-delete race).

    Two tasks on the same entity are already refused by grouping (one task per
    entity), but the SAME request twice inside one entity's list would make
    one task call ``delete_file`` on the same row twice: the second call's
    ``False`` is miscounted as ``failed``. Every namespace's helper runs this
    guard up front, before any I/O, so the identical script fails identically
    in validation, dry-run, and execute. Pure: no I/O.
    """
    seen: set[tuple[Any, ...]] = set()
    for deletion in deletions:
        key = _request_key(deletion)
        if key in seen:
            raise ValueError(
                "delete_entity_files_parallel: each deletion request may appear at most once per "
                f"call; duplicated: {deletion}. Two deletes of the same file row race each other - "
                "pass each target once."
            )
        seen.add(key)


def group_deletions_by_entity(deletions: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group one call's deletion requests by entity, in first-appearance order.

    One task runs per DISTINCT entity (its requests are handled sequentially
    inside that task — same-row safety), so different entities' deletes
    overlap while one entity's never race themselves. First-appearance order
    keeps the helper's result list deterministic and stable across modes.
    A request without a usable ``shared_id`` raises ``ValueError`` — every
    mode validates through this function, so a malformed request fails the
    identical script identically (never a silent drop, which would hide the
    loss of a deletion). Pure: no I/O.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for deletion in deletions:
        shared_id = deletion.get("shared_id")
        if not isinstance(shared_id, str) or not shared_id:
            raise ValueError(f"delete_entity_files_parallel: every deletion request must name a shared_id; got: {deletion}")
        grouped.setdefault(shared_id, []).append(deletion)
    return list(grouped.items())


def resolve_deletion_requests(
    shared_id: str,
    refs: list[FileRef],
    cited: set[str],
    deletions: list[dict[str, Any]],
) -> ResolvedDeletions:
    """Resolve ONE entity's deletion requests against its file refs (pure).

    ``refs`` are the entity's uploaded-file refs (:func:`extract_file_refs`
    output — documents + uploaded attachments; URL attachments are absent by
    construction). ``cited`` are the file ids its connections cite
    (:func:`cited_file_ids`). Per request: ``file_id`` wins when present;
    else ``originalname`` (+ optional ``kind``) must match exactly one ref;
    the resolved target must not be cited. Two requests resolving to the
    SAME file row raise ``ValueError`` (the double-delete race, loudly —
    before any of this entity's deletes is applied). Refusals are reported,
    never guessed. Pure: no I/O.
    """
    targets: list[FileRef] = []
    refusals: list[DeletionRefusal] = []
    resolved_ids: list[str] = []
    for deletion in deletions:
        ref = _resolve_one(refs, deletion)
        if ref is None:
            refusals.append(_refusal_for(shared_id, refs, deletion))
            continue
        if ref.file_id in cited:
            refusals.append(
                DeletionRefusal(
                    shared_id=shared_id,
                    file_id=ref.file_id,
                    originalname=ref.originalname,
                    kind=ref.kind,
                    reason="cited",
                )
            )
            continue
        if ref.file_id in resolved_ids:
            raise ValueError(
                f"delete_entity_files_parallel: two requests on entity {shared_id} resolve to the same "
                f"file {ref.file_id} - pass each target once (a second delete would race the row and "
                "be miscounted as failed)."
            )
        resolved_ids.append(ref.file_id)
        targets.append(ref)
    return ResolvedDeletions(targets=targets, refusals=refusals)


def refuse_unbackable_targets(
    shared_id: str, resolved: ResolvedDeletions, bytes_by_id: dict[str, bytes | None]
) -> ResolvedDeletions:
    """Move targets whose bytes cannot be fetched into ``unavailable`` refusals.

    The deletion core persists bytes BEFORE the delete call (the revert
    precondition — a crash between delete and backup is unrecoverable), so a
    target with no bytes in hand is refused instead of deleted. Pure: no
    I/O; the caller hands in the fetched-bytes map (``None`` = unfetchable).
    """
    targets = [ref for ref in resolved.targets if bytes_by_id.get(ref.file_id) is not None]
    refusals = list(resolved.refusals)
    for ref in resolved.targets:
        if bytes_by_id.get(ref.file_id) is None:
            refusals.append(
                DeletionRefusal(
                    shared_id=shared_id,
                    file_id=ref.file_id,
                    originalname=ref.originalname,
                    kind=ref.kind,
                    reason="unavailable",
                )
            )
    return ResolvedDeletions(targets=targets, refusals=refusals)


def _request_key(deletion: dict[str, Any]) -> tuple[Any, ...]:
    """The identity of one deletion request (its shared_id + naming fields)."""
    return (
        deletion.get("shared_id"),
        deletion.get("file_id"),
        deletion.get("originalname"),
        deletion.get("kind"),
    )


def _resolve_one(refs: list[FileRef], deletion: dict[str, Any]) -> FileRef | None:
    """Resolve one request to a single ref, or ``None`` when it cannot."""
    file_id = deletion.get("file_id")
    if isinstance(file_id, str) and file_id:
        for ref in refs:
            if ref.file_id == file_id:
                return ref
        return None
    originalname = deletion.get("originalname")
    if not isinstance(originalname, str) or not originalname:
        return None
    kind = deletion.get("kind")
    matches = [ref for ref in refs if ref.originalname == originalname and (kind is None or ref.kind == kind)]
    return matches[0] if len(matches) == 1 else None


def _refusal_for(
    shared_id: str,
    refs: list[FileRef],
    deletion: dict[str, Any],
) -> DeletionRefusal:
    """Build the refusal record for one unresolvable request (reason included)."""
    file_id = deletion.get("file_id")
    originalname = deletion.get("originalname")
    kind = deletion.get("kind")
    if isinstance(file_id, str) and file_id:
        # file_id is precise: present-but-unmatched means the row is not one of
        # this entity's uploaded files (absent, a URL attachment, or another
        # entity's file).
        return DeletionRefusal(
            shared_id=shared_id, file_id=file_id, originalname=originalname, kind=kind, reason="not_found"
        )
    if not isinstance(originalname, str) or not originalname:
        # No naming at all: nothing to resolve (a malformed request).
        return DeletionRefusal(shared_id=shared_id, reason="not_found")
    matches = [ref.file_id for ref in refs if ref.originalname == originalname and (kind is None or ref.kind == kind)]
    reason: RefusalReason = "ambiguous" if matches else "not_found"
    return DeletionRefusal(shared_id=shared_id, originalname=originalname, kind=kind, reason=reason, matches=matches)
