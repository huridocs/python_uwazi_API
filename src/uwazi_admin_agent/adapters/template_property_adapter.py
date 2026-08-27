"""``UwaziClient``-backed relationship-property-name lookup for delete-revert.

Delegates to ``UwaziClient.templates.get_by_id`` (``GET /api/templates`` with
in-process caching) and finds the ``relationship``-type property whose
``relationType`` matches and whose ``content`` is either unset or equals the
deleted entity's template id — the same ``(relationType, content)`` rule the
backend's ``buildRelationshipMetadata`` uses to re-derive metadata, so the name
we resolve is the one the entity-save path will read back. Does **not** modify
``uwazi_api``; imports the client only.
"""

from __future__ import annotations

from typing import override

from uwazi_admin_agent.ports.template_property_port import TemplatePropertyLookupPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.property_type import PropertyType


class UwaziTemplatePropertyLookup(TemplatePropertyLookupPort):
    """Resolve a relationship property name via a :class:`UwaziClient`'s templates."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    async def find_relationship_property_name(
        self,
        template_id: str,
        relation_type_id: str,
        content_id: str | None = None,
    ) -> str | None:
        template = self._client.templates.get_by_id(template_id)
        if template is None:
            return None
        for prop in (*template.properties, *template.common_properties):
            if prop.type != PropertyType.RELATIONSHIP:
                continue
            if str(prop.relationType or "") != str(relation_type_id or ""):
                continue
            if prop.content and content_id and str(prop.content) != str(content_id):
                continue
            return prop.name
        return None
