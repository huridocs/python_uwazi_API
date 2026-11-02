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


def label_id_map(values: list) -> dict[str, str]:
    """``{label: id}`` over a thesaurus value tree, including the qualified
    ``"Group: Child"`` keys built with ``qualify_label``. Groups are headings,
    not selectable."""
    label_map: dict[str, str] = {}
    _build_label_map(values, label_map)
    return label_map


def _build_label_map(values: list, label_map: dict[str, str], parent_label: str | None = None) -> None:
    for v in values:
        if v.values:
            # A group: its children are the selectable options. Recurse with the
            # group name as the parent so each child also gets a qualified
            # "Group: Child" key (the bare child label alone is ambiguous when
            # two groups share a child label).
            _build_label_map(v.values, label_map, v.label)
        else:
            label_map[v.label] = v.id
            if parent_label:
                label_map[qualify_label(parent_label, v.label)] = v.id
