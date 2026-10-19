"""Read/write an entity's publish + permission state (the raw write path cannot).

``POST /api/entities`` deliberately ignores ``published``/``permissions``, so
restoring a modified entity's raw is not enough to undo a publish/unpublish or
a permission change. This port is the separate seam that owns that state:
reads come from ``GET /api/entities?include=permissions`` and writes go to
``POST /api/entities/permissions`` (the endpoint Uwazi's own publish flow uses).
"""

from abc import ABC, abstractmethod

from uwazi_admin_agent.domain.publish_state import PublishState


class PublishStatePort(ABC):
    """Entity publish/permission state access for backup + restore."""

    @abstractmethod
    async def read(self, shared_id: str, language: str | None = None) -> PublishState:
        """Return the entity's current grants (raises when the entity is absent)."""
        ...

    @abstractmethod
    async def restore(self, shared_id: str, state: PublishState) -> None:
        """Replace the entity's grants with ``state.permissions`` (idempotent).

        Uwazi derives the published flag from the presence of the ``public``
        grant, so this single call restores both publishing and per-user grants.
        """
        ...
