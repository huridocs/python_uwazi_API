import asyncio
import os
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

_chat_storage_ttl = float(os.environ.get("CHAT_STORAGE_TTL_SECONDS", "86400"))
chat_storage = InMemoryChatStorage(ttl_seconds=_chat_storage_ttl)


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweep_interval = max(60.0, chat_storage._ttl_seconds / 4)

    async def _sweeper() -> None:
        while True:
            await asyncio.sleep(sweep_interval)
            chat_storage.evict_expired()

    sweeper_task = asyncio.create_task(_sweeper())
    try:
        yield
    finally:
        sweeper_task.cancel()
        chat_storage._sessions.clear()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def info():
    return sys.version


def _resolve_job_id(request: AIJobRequest, job_id: str | None) -> str:
    return job_id or request.job_id or str(uuid.uuid4())[:8]


def _start_job(job_id: str, request: AIJobRequest) -> AIJobResponse:
    chat_storage.evict_expired()
    wrong_context = "[Context: View Library, Document: Velásquez-Rodríguez v. Honduras]"
    if wrong_context in request.message:
        request.message = request.message.replace(wrong_context, "")
    request.message = request.message.strip()
    existing = chat_storage.get_session(job_id) if request.continuation else None
    if existing is not None:
        session = existing
    else:
        session = chat_storage.create_session(job_id)
    session.status = AIJobStatus.RUNNING
    session.add_message("user", request.message)

    asyncio.create_task(_run_agent(job_id, request))

    return AIJobResponse(job_id=job_id, message=request.message, status=AIJobStatus.PENDING)


@app.post("/api/v1/jobs")
async def create_job(request: AIJobRequest) -> AIJobResponse:
    return _start_job(_resolve_job_id(request, None), request)


@app.post("/api/v1/jobs/{job_id}")
async def create_job_with_id(job_id: str, request: AIJobRequest) -> AIJobResponse:
    request.continuation = True
    return _start_job(_resolve_job_id(request, job_id), request)


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
        transcript=session.get_transcript(),
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

        context = session.get_transcript()
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
