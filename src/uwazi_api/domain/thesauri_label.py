"""Helpers for qualifying grouped thesaurus values as ``"Group: Child"``.

A Uwazi thesaurus can organise its values into named groups; a group is a
heading whose children are the selectable options. Two groups may share a child
label (e.g. ``HRC`` and ``Inter-American Commission`` both have a ``Resolution``
child), so the bare child label is ambiguous. These helpers qualify a child with
its group so the LLM can disambiguate, and parse a qualified label back into its
``(group, child)`` parts.

The separator is ``": "`` (colon + space) to match the natural phrasing
``"HRC: Resolution"``. It is used consistently on both the read side (entity
metadata is resolved to qualified labels) and the write/filter side (qualified
labels are resolved back to the child value id).
"""

THESAURUS_GROUP_SEPARATOR = ": "


def qualify_label(group: str, child: str) -> str:
    """Return ``"Group: Child"`` for a grouped value."""
    return f"{group}{THESAURUS_GROUP_SEPARATOR}{child}"


def split_label(label: str) -> tuple[str | None, str]:
    """Split a (possibly qualified) label into ``(group, child)``.

    Returns ``(None, label)`` when the label is not qualified.
    """
    if THESAURUS_GROUP_SEPARATOR in label:
        group, _, child = label.partition(THESAURUS_GROUP_SEPARATOR)
        return group, child
    return None, label
