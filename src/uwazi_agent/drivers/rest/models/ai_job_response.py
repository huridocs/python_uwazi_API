from pydantic import BaseModel

from uwazi_agent.drivers.rest.models.ai_job_status import AIJobStatus


class AIJobResponse(BaseModel):
    job_id: str | None = None
    message: str
    status: AIJobStatus
