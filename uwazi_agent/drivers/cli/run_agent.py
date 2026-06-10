import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from uwazi_agent.adapters.llm.openrouter_adapter import OpenRouterAdapter
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.use_cases.run_agent_use_case import RunAgentUseCase


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(PROJECT_ROOT / ".env")

UWAZI_URL = os.environ["UWAZI_URL"]
UWAZI_USER = os.environ["UWAZI_USER"]
UWAZI_PASSWORD = os.environ["UWAZI_PASSWORD"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]


async def main() -> None:
    uwazi_api = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    llm = OpenRouterAdapter(api_key=OPENROUTER_API_KEY)
    use_case = RunAgentUseCase(
        llm=llm,
        thesauri_api=uwazi_api,
        template_api=uwazi_api,
        template_mapper=uwazi_api.template_mapper,
        entity_api=uwazi_api,
        page_api=uwazi_api,
    )

    context_parts = []

    while True:
        print("\n--- Enter your task (or press Enter or type 'exit' to quit) ---")
        task = input("Task: ").strip()
        if not task or task.lower() == "exit":
            break

        context = "\n\n".join(context_parts) if context_parts else ""

        print("Sending task to OpenRouter via RunAgentUseCase...\n")
        result = await use_case.execute(task_description=task, context=context)
        print("=== Agent output ===")
        print(result.output)
        if result.thinking:
            print("\n=== Agent thinking ===")
            print(result.thinking)

        context_parts.append(f"Previous task: {task}\nPrevious answer: {result.output}")


if __name__ == "__main__":
    asyncio.run(main())
