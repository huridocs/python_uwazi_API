"""Pure transform from a snapshot raw entity to a POST /api/entities create payload.

Delete-revert re-creates a deleted entity via the **create branch** of
``POST /api/entities``: when the posted body has no ``sharedId``, Uwazi mints a
fresh ``sharedId``/``_id`` (the update branch, used for modified entities,
requires the original ``_id``+``sharedId`` and 422s on a deleted row). The create
branch's Zod schema (``CreateEntitySchema``) only accepts a fixed set of data
fields and strips the rest, so to keep the re-created entity's data as close to
the original as possible we preserve exactly those accepted fields and drop
identity + server-managed + unsupported fields.

Accepted by create (max data fidelity for what create can restore):
``title``, ``template``, ``icon``, ``user``, ``metadata``, ``attachments``.

Dropped (cannot be restored via the create path — known limitations):
``_id``, ``sharedId`` (re-minted by Uwazi), ``language`` (create takes the
request locale), ``relations`` (relationships torn down on delete — separate
limitation), ``documents``/``file`` (uploaded files — would need re-upload),
``generatedToc``, ``propertySelections``, ``published`` (new entity starts in
Uwazi's default publish state), ``creationDate``/``editDate`` (server-managed).

Pure: no I/O; the unit-test target for the delete-revert payload transform.
"""

from __future__ import annotations

from typing import Any

# Fields the POST /api/entities create branch accepts (CreateEntitySchema).
# Keep these from the snapshot raw so the re-created entity carries the
# original data; everything else is dropped (identity is re-minted by Uwazi,
# and relations/documents/etc. cannot be restored via create).
_CREATE_ALLOWED_FIELDS: frozenset[str] = frozenset({"title", "template", "icon", "user", "metadata", "attachments"})


def to_create_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a ``POST /api/entities`` create payload from a snapshot raw entity.

    Keeps only the data fields the create branch accepts (max fidelity for what
    create can restore) and drops identity + server-managed + unsupported fields.
    A deleted entity re-created from this payload gets a fresh ``sharedId``/``_id``;
    its title, template, icon, user, metadata and url-attachments are restored.

    Pure: returns a new dict; the kept values are referenced (not deep-copied) —
    safe because the payload is JSON-serialized on POST and never mutated here.
    """
    return {key: raw[key] for key in _CREATE_ALLOWED_FIELDS if key in raw}
