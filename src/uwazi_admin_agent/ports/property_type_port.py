"""Read a template's PROPERTY TYPES (the Uwazi type of each property).

The recording path needs to know whether a property's value is stored PER
LANGUAGE ROW (the free-text types) or is ONE value shared by every language
row (everything else). That distinction decides how many review rows a change
produces: a multiselect change is ONE change, while a translated paragraph is
one change PER LANGUAGE.

The port is the seam for reading those types from the instance's template
definitions. Optional injection (``None`` ⇒ the caller falls back to the
value's own shape, which agrees with the rule for every type the write
contract writes as a bare value), so a recording pass never depends on it.
"""

from abc import ABC, abstractmethod


class PropertyTypeLookupPort(ABC):
    """Resolve a template's ``{property_name: property_type}`` map."""

    @abstractmethod
    def property_types(self, template_id_or_name: str) -> dict[str, str]:
        """Return ``{property_name: property_type}`` for one template (``{}`` if unknown).

        The types are the lowercase Uwazi names the template definitions
        carry ("text", "multiselect", "date", ...). ``{}`` (never a partial
        guess) for an unknown template: the caller keeps the fallback it had.
        """
        ...


__all__ = ["PropertyTypeLookupPort"]
