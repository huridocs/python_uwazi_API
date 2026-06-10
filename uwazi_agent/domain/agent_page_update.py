from pydantic import BaseModel


class AgentPageUpdate(BaseModel):
    """A partial update to an existing page, identified by ``shared_id``.

    Only the fields you set are changed; any field left as ``None`` is kept
    as-is on Uwazi's side. To clear the JavaScript, pass an empty string.
    """

    shared_id: str
    title: str | None = None
    content: str | None = None
    javascript: str | None = None
    entity_view: bool | None = None
    language: str = "en"
