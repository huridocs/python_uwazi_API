"""``UwaziClient``-backed template label (display name) lookup.

Delegates to ``UwaziClient.templates`` (``GET /api/templates``, cached per
client) and resolves an id — what a raw entity's ``template`` field holds — to
the template's ``name``, which is what Uwazi shows a person and therefore what
every operator-facing surface must display. Falls back to the value it was given
when the template is unknown, so the label path degrades to today's behaviour
instead of blanking a column. Does **not** modify ``uwazi_api``; imports the
client only.
"""

from __future__ import annotations

from typing import override

from uwazi_admin_agent.ports.template_label_port import TemplateLabelPort
from uwazi_api.client import UwaziClient


class UwaziTemplateLabels(TemplateLabelPort):
    """Resolve a template id/name to its display label via a :class:`UwaziClient`."""

    def __init__(self, client: UwaziClient) -> None:
        self._client = client

    @override
    def label_for(self, template_id_or_name: str) -> str | None:
        value = str(template_id_or_name or "").strip()
        if not value:
            return None
        template = self._client.templates.get_by_id(value) or self._client.templates.get_by_name(value)
        # An unknown id keeps its own text: better a raw id on screen than an
        # empty cell that reads as "no template".
        return (template.name if template is not None else None) or value


__all__ = ["UwaziTemplateLabels"]
