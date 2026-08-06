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

    model_config = {
        "json_schema_extra": {
            "description": (
                "``published`` is a READ-ONLY mirror of Uwazi's stored flag; it is "
                "NOT a publication control. Visibility (public vs. logged-in only) in "
                "Uwazi is governed by the entity's ``permissions`` array (a public "
                "read permission means published). Setting ``published`` here has no "
                "side effect on the server: it is preserved on read and otherwise "
                "ignored on write. To publish or unpublish an entity, call the "
                "dedicated tool (``set_entities_publish_status``), or, in the Python "
                "agent, the ``publish_entities`` / ``unpublish_entities`` helpers."
            )
        }
    }
