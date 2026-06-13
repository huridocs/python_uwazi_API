from pydantic import BaseModel


class AgentPageCreate(BaseModel):
    """A brand-new page to be created in Uwazi.

    Mirrors :class:`AgentPage` but intentionally omits ``shared_id``: it is
    minted by Uwazi on creation and is only known afterwards (it is returned
    via :class:`AgentPageMutationResult`).

    Provide at least a markdown/HTML ``content`` body. ``javascript`` is
    optional and stored in the page's "Javascript" tab. ``css`` is optional
    and stored in ``metadata.css``; Uwazi injects it as a ``<style>`` tag in
    the document head, which is the right home for page-scoped CSS — putting
    ``<style>`` blocks inside ``content`` can confuse the React 18 hydration
    walker when it parses the body.
    """

    title: str
    content: str = ""
    javascript: str | None = None
    css: str | None = None
    language: str = "en"
    entity_view: bool = False
