from pydantic import BaseModel, Field

from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.schema_store import SchemaStore


class UwaziAgentToolsDependencies(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    thesauri_api: ThesauriApiPort
    template_api: TemplateApiPort
    template_mapper: TemplateMapperPort
    entity_api: EntityApiPort | None = None
    page_api: PageApiPort | None = None
    entity_store: EntityStore = Field(default_factory=EntityStore)
    schema_store: SchemaStore = Field(default_factory=SchemaStore)
