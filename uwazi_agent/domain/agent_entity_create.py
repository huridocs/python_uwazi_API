from typing import Any

from pydantic import BaseModel, Field


class AgentEntityCreate(BaseModel):
    """A brand-new entity to be created in Uwazi.

    Mirrors :class:`AgentEntity` but intentionally omits ``shared_id``:
    the ``shared_id`` is assigned by Uwazi on creation and is only known
    afterwards (it is returned via ``AgentEntityMutationResult``).
    """

    title: str
    template_name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    language: str = "en"
    published: bool | None = None
