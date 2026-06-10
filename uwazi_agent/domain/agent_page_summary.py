from pydantic import BaseModel


class AgentPageSummary(BaseModel):
    """A compact page listing (no full body, to stay token-cheap)."""

    shared_id: str
    title: str
    language: str = "en"
    url: str | None = None
    has_markdown: bool = False
    has_javascript: bool = False
