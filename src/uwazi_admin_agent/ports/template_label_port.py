from abc import ABC, abstractmethod


class TemplateLabelPort(ABC):
    """Resolve a template's display label (its human-readable name) from an id or name.

    A raw entity carries ``template`` as the template's **id**, but every surface
    that shows a template to a person wants the label ("MATTER", not
    ``5bfbb1a0471dd0fc16ada146``): the review table's Template column, the run
    dialog's wording. Uwazi's ``/api/templates`` documents carry no separate
    ``label`` field — ``name`` IS the label — so resolving the id once against
    that (cached) list is the whole job.

    Optional injection (``None`` ⇒ callers fall back to the raw value they
    already hold, so a display path never depends on the lookup): an unknown or
    unreachable template resolves to ``None`` and the caller keeps what it had.
    No ``uwazi_api`` modification; imports the client only.
    """

    @abstractmethod
    def label_for(self, template_id_or_name: str) -> str | None:
        """Return the template's label for ``template_id_or_name``, or ``None`` if unknown.

        Accepts either form (id or name) so callers can pass a raw's ``template``
        field without caring which it is.
        """
        ...

    @abstractmethod
    def property_labels(self, template_id_or_name: str) -> dict[str, str]:
        """Return ``{property_name: property_label}`` for one template (``{}`` if unknown).

        A suggestion row names its property by the template's property NAME
        (``document_ref``), which is an identifier, not something an operator can
        read. The same document calls it "Document Ref" — the property's ``label``,
        which is what every Uwazi surface shows and what the review window has to
        show too. ``{}`` (never a partial guess) for an unknown template: the
        caller keeps the names it already has.
        """
        ...


__all__ = ["TemplateLabelPort"]
