"""Pure helpers for reading Uwazi metadata cells."""

from __future__ import annotations

from typing import Any, Mapping

from uwazi_api.domain.sanitize_property_label import PropertyLabelSanitizer


def metadata_text(value: Any) -> str:
    """Flatten a Uwazi metadata cell into a single non-empty string.

    A cell can be a scalar, a list of scalars, or a list of value envelopes
    (``{"value": ...}`` / ``{"label": ...}``) depending on the property type;
    the first non-empty piece is used.
    """
    if value is None:
        return ""
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, dict):
            inner = item.get("value")
            if isinstance(inner, dict):
                inner = inner.get("label") or inner.get("url")
            item = inner if inner is not None else item.get("label")
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


def _key_form(text: str) -> str:
    """Normalized comparison form of a property key or label.

    ``PropertyLabelSanitizer`` lowercases and maps non-alphanumerics to
    ``_`` (``"Document Ref"`` → ``"document_ref"``); dropping the ``_``
    afterwards also makes camelCase names (``documentRef``) equivalent.
    """
    return PropertyLabelSanitizer.sanitize(text).replace("_", "")


def metadata_value(metadata: Mapping[str, Any], property_name: str) -> Any:
    """Raw cell for ``property_name`` in a Uwazi metadata dict, or ``None``.

    Uwazi may key search-result metadata by property name, sanitized name,
    label, or sanitized label, so an exact ``dict.get`` is not enough: the
    configured name is matched case/format-insensitively against every key.
    An empty ``property_name`` disables the lookup.
    """
    if not property_name or not metadata:
        return None
    if property_name in metadata:
        return metadata[property_name]
    target = _key_form(property_name)
    for key, value in metadata.items():
        if _key_form(str(key)) == target:
            return value
    return None


def relationship_entries(value: Any) -> list[dict[str, str]]:
    """Normalize a relationship metadata cell to ``[{"shared_id", "title"}]``.

    Accepts the shapes Uwazi returns (``{"shared_id", "title"}`` dicts, the
    ``{"value": sharedId}`` envelope, plain shared ids, a single item or a
    list) and drops empty entries; the title falls back to the shared id.
    """
    items = value if isinstance(value, list) else [value]
    entries: list[dict[str, str]] = []
    for item in items:
        if isinstance(item, dict):
            shared_id = str(item.get("shared_id") or item.get("value") or "").strip()
            if not shared_id:
                continue
            title = str(item.get("title") or item.get("label") or shared_id)
            entries.append({"shared_id": shared_id, "title": title})
        elif item is not None and str(item).strip():
            shared_id = str(item).strip()
            entries.append({"shared_id": shared_id, "title": shared_id})
    return entries
