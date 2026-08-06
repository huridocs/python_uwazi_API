from pydantic import BaseModel


class AgentPageMenuLink(BaseModel):
    """An instruction to register a Settings → Links entry that points at a page.

    Use this after a page is created so that the page becomes reachable from
    the Uwazi navigation menu.

    Provide either:
        * ``shared_id`` of an existing page — the URL will be derived as
          ``/page/{shared_id}`` (with an optional ``slug`` appended), or
        * a complete ``url`` (e.g. ``/page/{shared_id}/welcome``).
    """

    title: str
    shared_id: str | None = None
    slug: str | None = None
    url: str | None = None
