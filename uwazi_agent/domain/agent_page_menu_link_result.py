from pydantic import BaseModel


class AgentPageMenuLinkResult(BaseModel):
    """Outcome of registering a single page in the Uwazi "Settings → Links" menu.

    The operation appends a new entry to the existing list, never replaces
    the whole list, so any other links that were already configured are
    preserved.
    """

    title: str
    url: str
    success: bool
    error: str | None = None
