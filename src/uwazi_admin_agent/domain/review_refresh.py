"""Pure rules for refreshing the review table and preflighting a Run.

The review table is a projection of Uwazi. A refresh re-runs the same script
against current state and merges its rows into the SAME attempt instead of
replacing the table. These functions decide what survives that merge, what a
stale Run looks like, and which snapshot value an undo restores. The store and
the UI consume them; neither re-decides the rules.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from uwazi_admin_agent.domain.metadata_diff import format_metadata_value
from uwazi_admin_agent.domain.suggestion import (
    CREATE_OPS,
    PROPERTY_OPS,
    TITLE_PROPERTY,
    Suggestion,
    SuggestionStatus,
)
from uwazi_admin_agent.domain.suggestion_scoring import value_in_language

# Rows a Run may still execute. APPLIED/DISCARDED rows are excluded by their
# own actions; OBSOLETE rows no longer match live Uwazi and must be refreshed
# before they can be run.
RUNNABLE_STATUSES: frozenset[SuggestionStatus] = frozenset(
    {
        SuggestionStatus.PENDING,
        SuggestionStatus.ACCEPTED,
        SuggestionStatus.EDITED,
        SuggestionStatus.REVERTED,
    }
)


class ReconciliationSummary(BaseModel):
    """What one in-place table refresh changed (pure value object)."""

    model_config = ConfigDict(frozen=True)

    inserted: int = Field(default=0, ge=0, description="New proposal rows added.")
    refreshed: int = Field(default=0, ge=0, description="Existing rows whose proposal columns changed.")
    status_reset: int = Field(default=0, ge=0, description="Existing rows returned to pending.")
    reappeared: int = Field(default=0, ge=0, description="Obsolete rows that are produced again.")
    obsolete: int = Field(default=0, ge=0, description="Rows no longer produced by the script.")
    kept_applied: int = Field(default=0, ge=0, description="Applied rows no longer produced but kept as history.")

    def summary_text(self) -> str:
        """One operator-facing line for a toast (pure)."""
        parts = [f"{self.inserted} new", f"{self.refreshed} refreshed"]
        if self.status_reset:
            parts.append(f"{self.status_reset} reset")
        if self.obsolete:
            parts.append(f"{self.obsolete} obsolete")
        if self.kept_applied:
            parts.append(f"{self.kept_applied} applied kept")
        return ", ".join(parts)


def suggestion_identity(
    shared_id: str,
    op: str,
    language: str | None,
    property_name: str | None,
    detail_key: str | None,
) -> tuple[str, str, str, str, str]:
    """The store's row identity, as a comparable tuple (pure)."""
    return (shared_id, op, language or "", property_name or "", detail_key or "")


def identity_of(suggestion: Suggestion) -> tuple[str, str, str, str, str]:
    """One suggestion's identity tuple (pure)."""
    return suggestion_identity(
        suggestion.shared_id,
        suggestion.op,
        suggestion.language,
        suggestion.property_name,
        suggestion.detail_key,
    )


def same_proposal(stored: Any, incoming: Any) -> bool:
    """Whether a re-run produced the SAME proposal value (pure)."""
    return stored == incoming


def reconciled_status(current: SuggestionStatus, *, same: bool, reappearing: bool = False) -> SuggestionStatus:
    """The status an existing row keeps after a refresh (pure).

    - a row that reappears after being OBSOLETE becomes PENDING again;
    - an unchanged proposal keeps the operator's status;
    - a changed proposal returns to PENDING, except that EDITED and DISCARDED
      are deliberate operator decisions and survive;
    - APPLIED is NOT special here: if the proposal changed, the old write no
      longer describes the table, so the row must be reviewed again.
    """
    if current is SuggestionStatus.OBSOLETE or reappearing:
        return SuggestionStatus.PENDING
    if same:
        return current
    if current in (SuggestionStatus.EDITED, SuggestionStatus.DISCARDED):
        return current
    return SuggestionStatus.PENDING


def missing_status(current: SuggestionStatus) -> SuggestionStatus:
    """The status of a row the refreshed script no longer produces (pure).

    An APPLIED write really happened and stays visible as history; everything
    else is retired as OBSOLETE because it can no longer be run against Uwazi.
    """
    return SuggestionStatus.APPLIED if current is SuggestionStatus.APPLIED else SuggestionStatus.OBSOLETE


def current_value_of(raw: dict[str, Any], property_name: str, language: str | None) -> Any:
    """The live value one row compares against, in its own language (pure)."""
    if property_name == TITLE_PROPERTY:
        return raw.get("title")
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return value_in_language(metadata.get(property_name), language)


def raw_answered_other_language(raw: dict[str, Any], language: str | None) -> bool:
    """True when Uwazi answered a missing row with another language's row (pure)."""
    raw_language = raw.get("language")
    return bool(language and isinstance(raw_language, str) and raw_language != language)


def restored_value(
    snapshot_raw: dict[str, Any],
    property_name: str,
    language: str | None = None,
) -> Any:
    """The value a snapshot restore puts back for one property (pure)."""
    if property_name == TITLE_PROPERTY:
        return snapshot_raw.get("title")
    metadata = snapshot_raw.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return value_in_language(metadata.get(property_name), language)


def display_equal(left: Any, right: Any) -> bool:
    """Compare two values the way the review table displays them (pure)."""
    return format_metadata_value(left) == format_metadata_value(right)


class PreflightIssue(BaseModel):
    """One selected row that cannot be run against current Uwazi state."""

    model_config = ConfigDict(frozen=True)

    shared_id: str
    property_name: str | None = None
    language: str | None = None
    kind: str = Field(description="'missing' (entity/row gone) or 'changed' (value moved).")

    def describe(self) -> str:
        target = self.property_name or "entity"
        language = f" [{self.language}]" if self.language else ""
        reason = "no longer exists" if self.kind == "missing" else "changed in Uwazi"
        return f"{self.shared_id}{language} · {target}: {reason}"


class PreflightResult(BaseModel):
    """The Run gate's outcome: okay, or the rows that need a table refresh."""

    model_config = ConfigDict(frozen=True)

    issues: list[PreflightIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def message(self, limit: int = 5) -> str:
        """A concise operator-facing explanation (pure)."""
        if self.ok:
            return "The selection still matches Uwazi."
        lines = [issue.describe() for issue in self.issues[:limit]]
        more = len(self.issues) - len(lines)
        if more > 0:
            lines.append(f"…and {more} more")
        return (
            "Run blocked: the table is out of date for this selection. "
            "Use 'Update table from Uwazi' first, then run again.\n" + "\n".join(lines)
        )


def evaluate_preflight(
    rows: list[Suggestion],
    raws: dict[tuple[str, str], dict[str, Any]],
    default_language: str,
) -> PreflightResult:
    """Compare selected rows' stored current values with live raws (pure).

    Creates have no live entity yet and are skipped. Every other row must still
    exist; a property row additionally compares the value it recorded at
    prediction time with the live value in its own language. Rows that are not
    runnable (applied/discarded/obsolete) are ignored — the Run would not push
    them.
    """
    issues: list[PreflightIssue] = []
    for row in rows:
        if row.status not in RUNNABLE_STATUSES:
            continue
        if not row.shared_id or row.shared_id.startswith("dry-created-"):
            continue
        if row.op in CREATE_OPS:
            continue
        language = row.language or default_language
        raw = raws.get((row.shared_id, language))
        if raw is None:
            issues.append(
                PreflightIssue(
                    shared_id=row.shared_id,
                    property_name=row.property_name,
                    language=row.language,
                    kind="missing",
                )
            )
            continue
        if row.property_name is None or row.op not in PROPERTY_OPS:
            continue
        live = (
            None
            if raw_answered_other_language(raw, row.language)
            else current_value_of(raw, row.property_name, row.language)
        )
        if not display_equal(row.current_value, live):
            issues.append(
                PreflightIssue(
                    shared_id=row.shared_id,
                    property_name=row.property_name,
                    language=row.language,
                    kind="changed",
                )
            )
    return PreflightResult(issues=issues)


__all__ = [
    "PreflightIssue",
    "PreflightResult",
    "ReconciliationSummary",
    "RUNNABLE_STATUSES",
    "current_value_of",
    "display_equal",
    "evaluate_preflight",
    "identity_of",
    "missing_status",
    "raw_answered_other_language",
    "reconciled_status",
    "restored_value",
    "same_proposal",
    "suggestion_identity",
]
