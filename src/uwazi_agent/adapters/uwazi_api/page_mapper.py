from typing import Optional

from uwazi_agent.domain.agent_page import AgentPage
from uwazi_agent.domain.agent_page_summary import AgentPageSummary
from uwazi_api.domain.page import Page


class PageMapper:
    """Maps between the Uwazi API ``Page`` and the agent-facing page models.

    Pages store their body in ``metadata.content`` (markdown/HTML) and their
    optional custom code in ``metadata.script`` (the UI's "Javascript" tab).
    """

    def __init__(self, base_url: Optional[str] = None):
        self._base_url = (base_url or "").rstrip("/")

    def page_url(self, page: Page) -> Optional[str]:
        if not self._base_url or not page.shared_id:
            return None
        return f"{self._base_url}/page/{page.shared_id}"

    def to_agent(self, page: Page) -> AgentPage:
        metadata = page.metadata or {}
        return AgentPage(
            shared_id=page.shared_id or "",
            title=page.title or "",
            language=page.language or "en",
            content=metadata.get("content") or "",
            javascript=metadata.get("script"),
            css=metadata.get("css"),
            entity_view=page.entity_view,
            url=self.page_url(page),
        )

    def to_summary(self, page: Page) -> AgentPageSummary:
        metadata = page.metadata or {}
        return AgentPageSummary(
            shared_id=page.shared_id or "",
            title=page.title or "",
            language=page.language or "en",
            url=self.page_url(page),
            has_markdown=bool(metadata.get("content")),
            has_javascript=bool(metadata.get("script")),
        )
