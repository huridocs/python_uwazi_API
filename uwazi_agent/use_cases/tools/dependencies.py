from pydantic import BaseModel

from uwazi_agent.ports.template_api_port import TemplateApiPort
from uwazi_agent.ports.template_mapper_port import TemplateMapperPort
from uwazi_agent.ports.thesauri_api_port import ThesauriApiPort
from uwazi_agent.ports.thesauri_mapper_port import ThesauriMapperPort


class UwaziAgentToolsDependencies(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    thesauri_api: ThesauriApiPort
    template_api: TemplateApiPort
    template_mapper: TemplateMapperPort
    thesauri_mapper: ThesauriMapperPort | None = None
