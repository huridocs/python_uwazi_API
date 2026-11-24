"""Look up the entities a ``relationship`` property can point at.

A relationship property is scoped by its template definition: ``content`` names
the related TEMPLATE (absent = any template) and ``relationType`` names the
relation type. The review surfaces need the candidates' TITLES (a shared id
alone means nothing to the operator) and the ability to search them: an
instance can carry hundreds of candidates, so the picker filters server-side
instead of shipping every title to the browser.

Implementations resolve the property from the template definitions, enumerate
the related entities through Uwazi's search index, and resolve titles for ids
already stored in a proposal. Every failure path returns an empty result — a
display lookup must never block the review page.

Synchronous by contract, like the template/thesaurus lookups the review page
already calls from its render path (the underlying ``requests`` calls are
blocking; NiceGUI runs handlers off the event loop).
"""

from abc import ABC, abstractmethod


class RelationshipCatalogPort(ABC):
    """Candidate targets (and titles) of one template's relationship property."""

    @abstractmethod
    def candidates(
        self,
        template_name: str,
        property_name: str,
        *,
        search: str | None = None,
        limit: int = 50,
        language: str | None = None,
    ) -> list[tuple[str, str]]:
        """``[(shared_id, title)]`` the property may link to, filtered by ``search``.

        ``search`` is a free-text query over the related template's entities
        (``None`` = the most recently created ``limit`` entities, sorted by
        title for display). ``language`` picks the locale the titles are read
        in; ``None`` uses the instance default. ``[]`` when the template or
        property cannot be resolved, or the read fails.
        """
        ...

    @abstractmethod
    def titles_for(self, shared_ids: list[str], language: str | None = None) -> dict[str, str]:
        """``{shared_id: title}`` for ids already recorded in a proposal.

        Ids that no longer resolve are simply absent from the map (the caller
        falls back to the id). ``{}`` for an empty list.
        """
        ...


__all__ = ["RelationshipCatalogPort"]
