from abc import ABC, abstractmethod


class TemplatePropertyLookupPort(ABC):
    """Resolve a relationship property NAME on a template by (relationType, content).

    Delete-revert's inbound-ref restore (``ReapplyRelationshipRefsAction``) must
    set a still-existing entity's in-metadata ``relationship``-property ref to
    the re-created entity's NEW sharedId. The property is identified by the
    captured hub's ``relationType`` (and, when the property is scoped to a
    related template, that ``content`` id) — mirroring the backend's
    ``buildRelationshipMetadata`` ``(relationType, content)`` match — but the
    entity-save path (``saveEntityBasedReferences`` → ``determinePropertyValues``)
    reads the metadata by property **name**, so the name must be resolved from
    the existing entity's template. This port is that seam.

    Optional injection (``None`` ⇒ inbound restore is skipped + warned, mirroring
    the file-restore precedent): a missing lookup surfaces as a relationship gap
    in post-revert verification rather than aborting the revert. No
    ``uwazi_api`` modification; imports the client only.
    """

    @abstractmethod
    async def find_relationship_property_name(
        self,
        template_id: str,
        relation_type_id: str,
        content_id: str | None = None,
    ) -> str | None:
        """Return the property ``name`` on ``template_id`` for the given relation type.

        Matches a ``relationship``-type property whose ``relationType`` equals
        ``relation_type_id`` and whose ``content`` is either unset or equals
        ``content_id`` (the deleted entity's template, when known). Returns
        ``None`` when no such property exists (the ref cannot be restored by the
        entity-save path; verification flags the gap).
        """
        ...
