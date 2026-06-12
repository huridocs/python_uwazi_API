from pathlib import Path

from pydantic import BaseModel, Field

from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.page_api_port import PageApiPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort
from uwazi_agent.ports.relationship_type_api_port import RelationshipTypeApiPort
from uwazi_agent.ports.settings_api_port import SettingsApiPort
from uwazi_agent.ports.stats_api_port import StatsApiPort
from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.schema_store import SchemaStore
from uwazi_agent.use_cases.tools.tool_call_cache import ToolCallCache


class UwaziAgentToolsDependencies(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    thesauri_api: ThesauriApiPort
    template_api: TemplateApiPort
    template_mapper: TemplateMapperPort
    stats_api: StatsApiPort | None = None
    relationship_type_api: RelationshipTypeApiPort | None = None
    relationship_api: RelationshipApiPort | None = None
    entity_api: EntityApiPort | None = None
    page_api: PageApiPort | None = None
    settings_api: SettingsApiPort | None = None
    page_builder_dir: Path | None = None
    entity_store: EntityStore = Field(default_factory=EntityStore)
    schema_store: SchemaStore = Field(default_factory=SchemaStore)
    tool_cache: ToolCallCache = Field(default_factory=ToolCallCache)
    tool_progress: list[str] = Field(default_factory=list)
