"""``UwaziClient``-backed template property-TYPE lookup.

Delegates to ``UwaziClient.templates`` (``GET /api/templates``, cached per
client) and projects a template's property list into ``{name: type}`` — the
lowercase Uwazi type name ("text", "multiselect", ...) the recording path
needs to tell a per-language property from a shared one.

The same cached read the template-label lookup already performs answers this,
so asking for the types costs no extra request. Does **not** modify
``uwazi_api``; imports the client only.
"""

from __future__ import annotations

from typing import Any, Iterable, override

from uwazi_admin_agent.ports.property_type_port import PropertyTypeLookupPort
from uwazi_api.client import UwaziClient


def property_type_map(properties: Iterable[Any]) -> dict[str, str]:
    """``{name: type}`` over a template's property list, skipping unnamed entries (pure).

    The projection is separated from the client so it is exercised with literal
    properties and no live instance. A property with no type of its own is
    skipped rather than mapped to a guess: an unknown type must not decide
    whether a value is per-language.
    """
    types: dict[str, str] = {}
    for prop in properties or ():
        name = str(getattr(prop, "name", "") or "")
        if not name:
            continue
        raw_type = getattr(prop, "type", None)
        value = str(getattr(raw_type, "value", None) or raw_type or "").strip().lower()
        if not value:
            continue
        types[name] = value
    return types


class UwaziTemplatePropertyTypes(PropertyTypeLookupPort):
    """Resolve a template id/name to its ``{property_name: property_type}`` map."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    def property_types(self, template_id_or_name: str) -> dict[str, str]:
        value = str(template_id_or_name or "").strip()
        if not value:
            return {}
        template = self._client.templates.get_by_id(value) or self._client.templates.get_by_name(value)
        if template is None:
            return {}
        return property_type_map([*template.properties, *template.common_properties])


__all__ = ["UwaziTemplatePropertyTypes", "property_type_map"]
