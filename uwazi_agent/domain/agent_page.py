from pydantic import BaseModel


class AgentPage(BaseModel):
    """A Settings → Pages entry in Uwazi, in agent-friendly terms.

    A page always carries a markdown/HTML body (``content``) and may also
    carry custom JavaScript (``javascript``) that runs on the public page.
    """

    shared_id: str
    title: str
    language: str = "en"
    content: str = ""
    javascript: str | None = None
    entity_view: bool = False
    url: str | None = None
