import asyncio
import sys
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from loguru import logger

from uwazi_agent.adapters.llm.ollama_adapter import OllamaAdapter
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.drivers.rest.models.ai_job_request import AIJobRequest
from uwazi_agent.drivers.rest.models.ai_job_response import AIJobResponse
from uwazi_agent.drivers.rest.models.ai_job_status import AIJobStatus
from uwazi_agent.drivers.rest.models.ai_job_status_response import AIJobStatusResponse
from uwazi_agent.drivers.rest.services.chat_storage import InMemoryChatStorage
from uwazi_agent.logging_config import setup_logging
from uwazi_agent.use_cases.run_agent_use_case import RunAgentUseCase

chat_storage = InMemoryChatStorage()


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    chat_storage._sessions.clear()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def info():
    return sys.version


@app.post("/api/v1/jobs")
async def create_job(request: AIJobRequest) -> AIJobResponse:
    job_id = request.job_id or str(uuid.uuid4())[:8]

    session = chat_storage.create_session(job_id)
    session.status = AIJobStatus.RUNNING
    session.add_message("user", request.message)

    asyncio.create_task(_run_agent(job_id, request))

    return AIJobResponse(job_id=job_id, message=request.message, status=AIJobStatus.PENDING)


@app.get("/api/v1/jobs/{job_id}")
async def get_job(job_id: str) -> AIJobStatusResponse:
    session = chat_storage.get_session(job_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Job not found")

    result = session.result
    if session.status in (AIJobStatus.PENDING, AIJobStatus.RUNNING):
        progress = session.progress
        result = progress[-1] if progress else "Task is starting..."

    return AIJobStatusResponse(
        job_id=session.job_id,
        status=session.status,
        result=result,
    )


@app.delete("/api/v1/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    session = chat_storage.get_session(job_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Job not found")

    chat_storage.delete_session(job_id)
    return {"job_id": job_id, "status": "deleted"}


async def _run_agent(job_id: str, request: AIJobRequest) -> None:
    session = chat_storage.get_session(job_id)
    if session is None:
        return

    setup_logging(url=request.credentials.url, user=request.credentials.username)

    try:
        uwazi_api = UwaziApiAdapter(
            user=request.credentials.username,
            password=request.credentials.password,
            url=request.credentials.url,
        )
        llm = OllamaAdapter()

        use_case = RunAgentUseCase(
            llm=llm,
            thesauri_api=uwazi_api,
            template_api=uwazi_api,
            template_mapper=uwazi_api.template_mapper,
            entity_api=uwazi_api,
            page_api=uwazi_api,
            relationship_type_api=uwazi_api,
            settings_api=uwazi_api,
            stats_api=uwazi_api,
        )

        context = session.get_context()
        result = await use_case.execute(
            task_description=request.message,
            context=context,
            tool_progress=session.progress,
        )

        session.add_message("assistant", result.output)
        session.result = result.output
        session.status = AIJobStatus.COMPLETED
        logger.info("JOB COMPLETED: job_id={} prompt={}", job_id, request.message[:200])

    except Exception as e:
        session.result = f"Error: {str(e)}"
        session.status = AIJobStatus.FAILED
        logger.error("JOB FAILED: job_id={} prompt={} error={}", job_id, request.message[:200], e)
