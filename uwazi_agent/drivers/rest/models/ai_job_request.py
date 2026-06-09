from pydantic import BaseModel

from uwazi_agent.drivers.rest.models.uwazi_credentials import UwaziCredentials


class AIJobRequest(BaseModel):
    job_id: str | None = None
    message: str
    credentials: UwaziCredentials
