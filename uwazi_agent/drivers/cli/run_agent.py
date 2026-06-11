import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from uwazi_agent.adapters.llm.ollama_adapter import OllamaAdapter
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.logging_config import setup_logging
from uwazi_agent.use_cases.run_agent_use_case import RunAgentUseCase


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

UWAZI_URL = os.environ["UWAZI_URL"]
UWAZI_USER = os.environ["UWAZI_USER"]
UWAZI_PASSWORD = os.environ["UWAZI_PASSWORD"]


MAX_CONTEXT_EXCHANGES = 10


async def main() -> None:
    setup_logging(url=UWAZI_URL, user=UWAZI_USER)

    uwazi_api = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
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

    context_parts: list[str] = []

    while True:
        print("\n--- Enter your task (or press Enter or type 'exit' to quit) ---", file=sys.stderr)
        print("Task: ", end="", file=sys.stderr, flush=True)
        task = input().strip()
        if not task or task.lower() == "exit":
            break

        context = "\n\n".join(context_parts) if context_parts else ""

        print("Sending task to OpenRouter via RunAgentUseCase...\n")
        result = await use_case.execute(task_description=task, context=context)

        sys.stderr.flush()
        print("\n=== Agent output ===")
        print(result.output)
        if result.thinking:
            print("\n=== Agent thinking ===")
            print(result.thinking)

        sys.stdout.flush()

        context_parts.append(f"Previous task: {task}\nPrevious answer: {result.output}")
        if len(context_parts) > MAX_CONTEXT_EXCHANGES:
            context_parts = context_parts[-MAX_CONTEXT_EXCHANGES:]


if __name__ == "__main__":
    asyncio.run(main())
