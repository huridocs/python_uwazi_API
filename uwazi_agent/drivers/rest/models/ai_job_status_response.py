from pydantic import BaseModel

from uwazi_agent.drivers.rest.models.ai_job_status import AIJobStatus


class AIJobStatusResponse(BaseModel):
    job_id: str
    result: str | None = None
    status: AIJobStatus
