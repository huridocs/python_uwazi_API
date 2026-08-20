from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EntityIdentity(BaseModel):
    """Lightweight identity on an entity, used in manifest entries.

    ``shared_id`` is required - Uwazi always assigns a ``sharedId``; a missing
    one is a loud error, not a slient ``None`` (§8 changelog).
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The Uwazi sharedId; required.")
    internal_id: str | None = Field(default=None, description="The Uwazi _id, when known.")
    language: str | None = Field(default=None, description="The row locale, when known.")
    restored_shared_id: str | None = Field(
        default=None,
        description=(
            "For a deleted entity, the NEW sharedId Uwazi minted when revert "
            "re-created it via the create branch. Set by RevertRunUseCase after "
            "re-create so post-revert verification can fetch the re-created row; "
            "None for modified/created entries and for not-yet-reverted runs."
        ),
    )


class EntitySnapshot(BaseModel):
    """The exact raw entity JSON for one entity, captured at backup time.

    ``raw`` is the unmodified dict Uwazi returned - never a validated model -
    so round-tripping drops no fields (§2.5). Identity fields are carried
    directly (not via an embedded ``EntityIdentity``) so a snapshot is
    self-contained for restore.
    """

    model_config = ConfigDict(frozen=True)

    shared_id: str = Field(description="The Uwazi sharedId; required (§8 changelog).")
    internal_id: str | None = Field(default=None, description="The Uwazi _id, when known.")
    language: str | None = Field(default=None, description="The row locale, when known.")
    raw: dict[str, Any] = Field(description="The exact raw entity JSON Uwazi returned.")
    captured_at: datetime = Field(description="When the snapshot was captured.")
