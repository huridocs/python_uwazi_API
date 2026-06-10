from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_entity import AgentEntity


class EntityStore(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    entities: list[AgentEntity] = Field(default_factory=list)
    needs_python_agent: bool = False

    def add_entities(self, entities: list[AgentEntity]) -> None:
        existing_ids = {e.shared_id for e in self.entities}
        for entity in entities:
            if entity.shared_id not in existing_ids:
                self.entities.append(entity)
                existing_ids.add(entity.shared_id)

    def clear(self) -> None:
        self.entities.clear()
        self.needs_python_agent = False
