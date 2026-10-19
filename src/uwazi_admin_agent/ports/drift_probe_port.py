"""Drift probe port: read Uwazi's editDate signals without full entity re-reads.

Two queries, both served from Elasticsearch (``/api/v2/search``):

* ``probe`` — the top N entities of one template sorted by ``editDate`` (the
  indexed ``editDate.sort`` sub-field), each hit reduced to
  ``(sharedId, editDate)``. Answering "did anything change recently?" costs
  one small request per covered language.
* ``scan`` — the same query paged across the WHOLE template (~10k per page),
  for the full baseline-diff. Only sharedId/editDate are read, so a 10k
  scan is a couple of requests of tiny payloads — no titles, metadata or
  relationships.
* ``count`` — ``GET /api/v2/entities/count_by_template`` (a Mongo count),
  which catches deletions ``editDate`` cannot see.

The language dimension follows the search API's own semantics: the route
always applies a ``term: { language }`` filter from the request locale, so
one call covers one language (same convention :class:`UwaziSearchProbe`
already uses with its ``Accept-Language`` header + ``locale`` cookie).
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DriftProbePort(ABC):
    """Read-only editDate signals over a Uwazi instance."""

    @abstractmethod
    def probe_recent(self, template_id: str, language: str, limit: int = 10) -> dict[str, int | None]:
        """The ``limit`` most-recently-edited entities of one template.

        Returns ``sharedId -> editDate`` (ms epoch; a hit without a parseable
        editDate maps to None). Failures raise — the caller decides whether a
        failed probe means 'unknown' or is fatal for the verdict.
        """
        ...

    @abstractmethod
    def scan_template(self, template_id: str, language: str, page_size: int = 10000) -> dict[str, int | None]:
        """Every entity of one template as ``sharedId -> editDate`` (paged).

        Pages until the scan is exhausted. Failures raise.
        """
        ...

    @abstractmethod
    def count_by_template(self, template_id: str) -> int:
        """The template's entity count (``/api/v2/entities/count_by_template``)."""
        ...
