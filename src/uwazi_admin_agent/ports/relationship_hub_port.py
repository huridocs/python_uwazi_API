"""Raw ``connections`` (relationship-hub) rows: the write path the entity save cannot restore.

Uwazi's relationship hubs are rows of the ``connections`` collection. Rows that
carry a ``template`` (a relationship-type id) are the ones a relationship
*property* points at, and ``POST /api/entities`` rebuilds them from the posted
metadata. Rows that carry ``file`` (a file-cited connection) or ``reference``
(text citations) belong to NO property, so a metadata restore never recreates
them: a run that created such a citation could not undo it, and a revert left
it live.

This port is the raw seam for those rows: read a sharedId's own connection rows
with their hub siblings, and replay rows through ``POST /api/relationships/bulk``
(the same endpoint Uwazi's own reference save uses).
"""

from abc import ABC, abstractmethod
from typing import Any


class RelationshipHubPort(ABC):
    """Raw connection-row access for capturing + restoring non-property hubs."""

    @abstractmethod
    async def list_for_entity(self, shared_id: str, language: str | None = None) -> list[dict[str, Any]]:
        """Return every connection row in the hubs ``shared_id`` participates in.

        One entry per row (``_id``, ``hub``, ``entity``, ``template``, and
        ``file``/``reference`` when present). Empty when the entity has no
        connections.
        """
        ...

    @abstractmethod
    async def delete_rows(self, row_ids: list[str], language: str | None = None) -> None:
        """Delete connection rows by ``_id`` (``POST /api/relationships/bulk`` delete)."""
        ...

    @abstractmethod
    async def restore_rows(self, rows: list[dict[str, Any]], language: str | None = None) -> None:
        """Re-create connection rows (``POST /api/relationships/bulk`` save, grouped by hub)."""
        ...
