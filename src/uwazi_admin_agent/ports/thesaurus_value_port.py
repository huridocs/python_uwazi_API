"""Thesaurus value lookup port: which thesaurus ids a select property's labels map to."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ThesaurusValueLookupPort(ABC):
    """Resolve a select/multiselect property's labels to thesaurus value ids.

    Uwazi's ``UpdateEntitySchema.metadata`` requires ``[{value: <id>}]``
    envelopes and resolves values by id — a label either fails validation or is
    silently dropped — so the apply path must translate the proposal's labels
    into thesaurus value UUIDs before writing.

    Optional injection (``None`` ⇒ callers REFUSE the write instead of guessing,
    so a select/multiselect row is never applied with an unresolved mapping):
    an unknown template/property/thesaurus resolves to ``{}``.
    """

    @abstractmethod
    def value_ids(self, template_name: str, property_name: str) -> dict[str, str]:
        """``{label: value uuid}`` for one select/multiselect property of a template.

        ``{}`` when the template/property/thesaurus cannot be resolved.
        """
        ...


__all__ = ["ThesaurusValueLookupPort"]
