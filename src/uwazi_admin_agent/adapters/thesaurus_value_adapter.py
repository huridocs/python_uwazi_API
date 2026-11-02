"""``UwaziClient``-backed thesaurus label → value id lookup.

Resolves a template (by name or id), finds the select/multiselect property in
its ``properties`` + ``common_properties``, reads the property's ``content``
(the thesaurus id), then walks that thesaurus's value tree through
``label_id_map``. Any miss (unknown template, missing property, no content,
unknown thesaurus) returns ``{}`` — the caller refuses the write rather than
guessing. Does **not** modify ``uwazi_api``; imports the client only.

Labels are resolved untranslated in the instance's default language: thesaurus
value ids are language-independent, and the map keys are what
``records_to_suggestions`` stored in ``proposed_value``.
"""

from __future__ import annotations

from typing import override

from uwazi_admin_agent.ports.thesaurus_value_port import ThesaurusValueLookupPort
from uwazi_api.client import UwaziClient
from uwazi_api.domain.thesauri_label import label_id_map


class UwaziThesaurusValues(ThesaurusValueLookupPort):
    """Resolve labels of one template property to its thesaurus value ids."""

    def __init__(self, client: UwaziClient, language: str = "en") -> None:
        self._client: UwaziClient = client
        self._language: str = language

    @override
    def value_ids(self, template_name: str, property_name: str) -> dict[str, str]:
        name = str(template_name or "").strip()
        prop_name = str(property_name or "").strip()
        if not name or not prop_name:
            return {}
        template = self._client.templates.get_by_id(name) or self._client.templates.get_by_name(name)
        if template is None:
            return {}
        prop = next(
            (p for p in [*template.properties, *template.common_properties] if getattr(p, "name", None) == prop_name),
            None,
        )
        content = str(getattr(prop, "content", "") or "").strip() if prop is not None else ""
        if not content:
            if prop is None:
                # The property may have been renamed out of the template — but
                # Uwazi suffixes duplicate names, so also match the label.
                candidate = next(
                    (
                        p
                        for p in [*template.properties, *template.common_properties]
                        if str(getattr(p, "label", "") or "").strip() == prop_name
                        and str(getattr(p, "content", "") or "").strip()
                    ),
                    None,
                )
                prop = candidate
            if prop is None:
                return {}
            content = str(getattr(prop, "content", "") or "").strip()
            if not content:
                return {}
        for thesaurus in self._client.thesauris.get(language=self._language):
            if thesaurus.id == content:
                return label_id_map(thesaurus.values)
        return {}

    def set_language(self, language: str) -> None:
        self._language = str(language or "en").strip() or "en"


__all__ = ["UwaziThesaurusValues"]
