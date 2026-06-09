import random
import sys
from pathlib import Path

from fastapi import FastAPI

from uwazi_agent.drivers.rest.models.ai_job_request import AIJobRequest
from uwazi_agent.drivers.rest.models.ai_job_response import AIJobResponse
from uwazi_agent.drivers.rest.models.ai_job_status import AIJobStatus
from uwazi_agent.drivers.rest.models.ai_job_status_response import AIJobStatusResponse

app = FastAPI()

data_path = Path("data.json")
params_path = Path("params.json")
options_path = Path("options.json")

jobs_store: dict[str, AIJobStatusResponse] = {}


@app.get("/info")
async def info():
    return sys.version


@app.post("/api/v1/jobs")
async def create_job(request: AIJobRequest) -> AIJobResponse:
    job_id = request.job_id or str(random.randint(100000, 999999))
    result_markdown = "Using the **Template Inspector** tool to analyze the template structure."
    jobs_store[job_id] = AIJobStatusResponse(job_id=job_id, status=AIJobStatus.RUNNING, result=result_markdown)
    return AIJobResponse(job_id=job_id, message=request.message, status=AIJobStatus.PENDING)


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str) -> AIJobStatusResponse:
    job = jobs_store.get(job_id, AIJobStatusResponse(job_id=job_id, status=AIJobStatus.FAILED)).model_copy()
    if job.status == AIJobStatus.RUNNING:
        result_markdown = """
        ## Task Execution Summary

        I have completed the requested operation by leveraging the following tools:

        ### Tools Used

        1. **Template Inspector** — Loaded and parsed the target template definition to identify its structure and bound entities.
        2. **Entity Remover** — Removed the specified entities from the underlying data store, ensuring referential integrity was preserved.

        ### Results

        | Step | Tool | Status |
        |------|------|--------|
        | 1 | Template Inspector | Completed |
        | 2 | Entity Remover | Completed |

        > All operations finished successfully. No further action is required.
            """
        jobs_store[job_id] = AIJobStatusResponse(job_id=job_id, status=AIJobStatus.COMPLETED, result=result_markdown)

    return job
