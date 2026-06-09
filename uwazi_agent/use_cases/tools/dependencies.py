from pydantic import BaseModel

from uwazi_agent.ports.mapper_port import ThesauriMapperPort
from uwazi_agent.ports.uwazi_api_port import ThesauriApiPort


class ThesauriToolsDependencies(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    api: ThesauriApiPort
    mapper: ThesauriMapperPort | None = None
