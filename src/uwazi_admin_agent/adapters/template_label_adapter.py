"""``UwaziClient``-backed template label (display name) lookup.

Delegates to ``UwaziClient.templates`` (``GET /api/templates``, cached per
client) and resolves an id — what a raw entity's ``template`` field holds — to
the template's ``name``, which is what Uwazi shows a person and therefore what
every operator-facing surface must display. Falls back to the value it was given
when the template is unknown, so the label path degrades to today's behaviour
instead of blanking a column. Does **not** modify ``uwazi_api``; imports the
client only.

The same lookup answers a template's PROPERTY labels (``property_labels``): a
review row names its property by the template's property ``name``
(``document_ref``), which is an identifier, while Uwazi — and therefore the
operator — reads it as the property's ``label`` ("Document Ref"). Both come from
one cached ``/api/templates`` read, so adding the property map costs no extra
request.
"""

from __future__ import annotations

from typing import Any, Iterable, override

from uwazi_admin_agent.ports.template_label_port import TemplateLabelPort
from uwazi_api.client import UwaziClient


def property_label_map(properties: Iterable[Any]) -> dict[str, str]:
    """``{name: label}`` over a template's property list, skipping unnamed entries (pure).

    The projection is separated from the client so it is exercised with literal
    properties and no live instance. A property with no label of its own maps to
    its own name, which keeps the map total and never hands the UI an empty
    heading. Common properties are included by the caller: Uwazi stores the
    built-ins (``title``) in the same list shape.
    """
    labels: dict[str, str] = {}
    for prop in properties or ():
        name = str(getattr(prop, "name", "") or "")
        if not name:
            continue
        label = str(getattr(prop, "label", "") or "").strip()
        labels[name] = label or name
    return labels


class UwaziTemplateLabels(TemplateLabelPort):
    """Resolve a template id/name — and its property names — to display labels."""

    def __init__(self, client: UwaziClient) -> None:
        self._client: UwaziClient = client

    @override
    def label_for(self, template_id_or_name: str) -> str | None:
        value = str(template_id_or_name or "").strip()
        if not value:
            return None
        template = self._client.templates.get_by_id(value) or self._client.templates.get_by_name(value)
        # An unknown id keeps its own text: better a raw id on screen than an
        # empty cell that reads as "no template".
        return (template.name if template is not None else None) or value

    @override
    def property_labels(self, template_id_or_name: str) -> dict[str, str]:
        value = str(template_id_or_name or "").strip()
        if not value:
            return {}
        template = self._client.templates.get_by_id(value) or self._client.templates.get_by_name(value)
        if template is None:
            return {}
        return property_label_map([*template.properties, *template.common_properties])


__all__ = ["UwaziTemplateLabels", "property_label_map"]
