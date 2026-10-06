"""Label lifecycle: create/delete Uwazi relationships and notify subscribers.

The read path is a pure function (:func:`labels_from_relations`) that turns
Uwazi's denormalized ``relations`` view back into :class:`Label` objects, so it
is unit-testable offline. Persistence goes through :class:`UwaziPort` (the
relationships bulk endpoint); notification fans out to webhooks and extensions.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from uwazi_api.domain.reference import Reference
from uwazi_api.domain.selection_rectangle import SelectionRectangle
from uwazi_property_filler.domain.label import MANUAL_SOURCE, Label
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.ports.label_notify_port import LabelNotifyPort
from uwazi_property_filler.ports.uwazi_port import UwaziPort


def object_id(value: Any) -> str | None:
    """Extract a Mongo ObjectId string from a plain hex string or ``{"$oid": …}``."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        oid = value.get("$oid")
        return oid if isinstance(oid, str) and oid else None
    return None


def labels_from_relations(
    relations: list[dict[str, Any]],
    *,
    shared_id: str,
    file_id: str,
    relationship_type_id: str,
) -> list[Label]:
    """Reconstruct manual labels from Uwazi's denormalized ``relations``.

    A label is a hub with a FROM row (``template`` null, ``file`` == this
    document's file id, carrying the ``reference``) and a TO row (``template``
    == ``relationship_type_id``, ``entity`` == the label target).
    """
    from_by_hub: dict[str, dict[str, Any]] = {}
    to_by_hub: dict[str, dict[str, Any]] = {}
    for rel in relations:
        if not isinstance(rel, dict):
            continue
        hub = object_id(rel.get("hub"))
        if hub is None:
            continue
        if rel.get("template") is None:
            from_by_hub.setdefault(hub, rel)
        elif object_id(rel.get("template")) == relationship_type_id:
            to_by_hub.setdefault(hub, rel)

    labels: list[Label] = []
    for hub, from_rel in from_by_hub.items():
        to_rel = to_by_hub.get(hub)
        if to_rel is None:
            continue
        if object_id(from_rel.get("file")) != file_id:
            continue
        reference = from_rel.get("reference") or {}
        rectangles = _parse_rectangles(reference.get("selectionRectangles"))
        labels.append(
            Label(
                id=hub,
                shared_id=shared_id,
                file_id=file_id,
                label_shared_id=str(to_rel.get("entity") or ""),
                relationship_type_id=relationship_type_id,
                text=str(reference.get("text") or ""),
                selection_rectangles=rectangles,
                page=_first_page(rectangles),
                source=MANUAL_SOURCE,
                hub=hub,
            )
        )
    return labels


def _parse_rectangles(raw: Any) -> list[SelectionRectangle]:
    if not isinstance(raw, list):
        return []
    rectangles: list[SelectionRectangle] = []
    for item in raw:
        try:
            rectangles.append(SelectionRectangle.model_validate(item))
        except ValidationError:
            continue
    return rectangles


def _first_page(rectangles: list[SelectionRectangle]) -> int:
    if not rectangles:
        return 1
    try:
        return int(rectangles[0].page)
    except (TypeError, ValueError):
        return 1


async def create_label(
    uwazi: UwaziPort,
    label: Label,
    *,
    file_id: str,
    relationship_type_id: str,
    language: str,
) -> Label:
    """Persist a manual label as a Uwazi relationship, then return the enriched label."""
    reference = Reference(text=label.text, selection_rectangles=label.selection_rectangles)
    await uwazi.create_relationship(
        label.shared_id,
        file_id,
        reference,
        label.label_shared_id,
        relationship_type_id,
        language,
    )
    label.file_id = file_id
    label.relationship_type_id = relationship_type_id
    label.source = MANUAL_SOURCE
    label.confidence = None
    return label


async def delete_label(uwazi: UwaziPort, label: Label, *, language: str) -> None:
    """Delete a manual label's connection hub (no-op for predictions)."""
    if label.hub:
        await uwazi.delete_relationships([label.hub], language)


async def notify_label(
    notifiers: list[LabelNotifyPort],
    extensions: list[ExtensionPort],
    label: Label,
    *,
    deleted: bool,
) -> None:
    """Fan out a label change to webhooks and extension ``on_label`` hooks."""
    for notifier in notifiers:
        if deleted:
            await notifier.notify_deleted(label)
        else:
            await notifier.notify_created(label)
    for extension in extensions:
        await extension.on_label(label)
