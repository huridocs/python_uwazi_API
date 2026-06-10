from pydantic import BaseModel


class AgentPageCreate(BaseModel):
    """A brand-new page to be created in Uwazi.

    Mirrors :class:`AgentPage` but intentionally omits ``shared_id``: it is
    minted by Uwazi on creation and is only known afterwards (it is returned
    via :class:`AgentPageMutationResult`).

    Provide at least a markdown/HTML ``content`` body. ``javascript`` is
    optional and stored in the page's "Javascript" tab.
    """

    title: str
    content: str = ""
    javascript: str | None = None
    language: str = "en"
    entity_view: bool = False
