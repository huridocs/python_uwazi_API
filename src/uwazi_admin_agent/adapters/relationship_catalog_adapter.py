"""``UwaziClient``-backed relationship candidate lookup (see the port).

Resolves a template's ``relationship`` property, reads the related TEMPLATE it
is scoped to (``content``), and enumerates that template's entities through
Uwazi's search index — ``searchTerm`` narrows the list server-side, so a
property with hundreds of candidates is never shipped whole to the browser.
Titles for ids already in a proposal are read one by one (a proposal holds a
handful of ids, and the review page memoises the map).

Every failure path returns an empty result: a display lookup must never break
the review page. Does **not** modify ``uwazi_api``; imports the client only.
"""

from __future__ import annotations

from typing import override

from loguru import logger

from uwazi_admin_agent.ports.relationship_catalog_port import RelationshipCatalogPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.property_type import PropertyType

_DEFAULT_LANGUAGE: str = "en"


class UwaziRelationshipCatalog(RelationshipCatalogPort):
    """Relationship candidates over a :class:`UwaziClient`'s templates + search."""

    def __init__(self, client: UwaziClient, language: str = _DEFAULT_LANGUAGE) -> None:
        self._client: UwaziClient = client
        self._language: str = language

    @override
    def candidates(
        self,
        template_name: str,
        property_name: str,
        *,
        search: str | None = None,
        limit: int = 50,
        language: str | None = None,
    ) -> list[tuple[str, str]]:
        usable, related_template = self._related_template(template_name, property_name)
        if not usable:
            # The property is not a relationship (or is unresolvable): there is
            # nothing to enumerate, and guessing "all entities" would offer
            # targets Uwazi would refuse.
            return []
        locale = language or self._language
        try:
            if search and search.strip():
                entities = self._client.search.search_by_text(
                    search_term=search.strip(),
                    template_name=related_template or None,
                    start_from=0,
                    batch_size=max(int(limit), 1),
                    language=locale,
                )
            else:
                entities = self._client.search.get(
                    start_from=0,
                    batch_size=max(int(limit), 1),
                    template_name=related_template or None,
                    language=locale,
                )
        except Exception as exc:  # noqa: BLE001 — a candidate read never blocks the picker
            logger.warning(
                "could not read relationship candidates for {}.{}: {}",
                template_name,
                property_name,
                exc,
            )
            return []
        found: list[tuple[str, str]] = []
        for entity in entities:
            shared_id = str(entity.shared_id or "").strip()
            if not shared_id:
                continue
            found.append((shared_id, str(entity.title or shared_id)))
        found.sort(key=lambda item: item[1].lower())
        return found

    @override
    def titles_for(self, shared_ids: list[str], language: str | None = None) -> dict[str, str]:
        locale = language or self._language
        titles: dict[str, str] = {}
        for shared_id in dict.fromkeys(shared_ids):
            if not shared_id:
                continue
            try:
                entity = self._client.entities.get_one(shared_id, locale)
            except Exception as exc:  # noqa: BLE001 — an unresolved id falls back to itself
                logger.debug("no title for related entity {} ({}): {}", shared_id, locale, exc)
                continue
            title = str(entity.title or "").strip()
            if title:
                titles[shared_id] = title
        return titles

    def _related_template(self, template_name: str, property_name: str) -> tuple[bool, str | None]:
        """``(usable, related template id)`` for the property.

        ``(True, None)`` means the property may link ANY template. ``(False,
        None)`` means the property/template cannot be resolved or the property
        is not a ``relationship`` — the caller then offers nothing rather than
        guessing targets Uwazi would refuse.

        Mirrors the thesaurus lookup's property resolution: match the property
        ``name`` first, then its label (Uwazi suffixes duplicate names).
        """
        name = str(template_name or "").strip()
        prop_name = str(property_name or "").strip()
        if not name or not prop_name:
            return False, None
        template = self._client.templates.get_by_id(name) or self._client.templates.get_by_name(name)
        if template is None:
            return False, None
        props = [*template.properties, *template.common_properties]
        prop = next((p for p in props if getattr(p, "name", None) == prop_name), None)
        if prop is None:
            prop = next(
                (p for p in props if str(getattr(p, "label", "") or "").strip() == prop_name),
                None,
            )
        if prop is None or prop.type != PropertyType.RELATIONSHIP:
            return False, None
        content = str(getattr(prop, "content", "") or "").strip()
        return True, content or None

    def set_language(self, language: str) -> None:
        self._language = str(language or _DEFAULT_LANGUAGE).strip() or _DEFAULT_LANGUAGE


__all__ = ["UwaziRelationshipCatalog"]
