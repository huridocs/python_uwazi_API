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

TASK_DESCRIPTION = (
    "List the names of all thesauri currently defined in this Uwazi instance, "
    "then create a new thesaurus called 'Driver Smoke Test Thesauri' with the "
    "values ['alpha', 'beta', 'gamma']. Report what you found and what you did."
)

CONTEXT = (
    "You are running inside the uwazi_agent CLI driver. The Uwazi adapter has "
    "already authenticated with the credentials from the .env file, so any "
    "tool you call will hit a real instance. Be concise and prefer reading "
    "before writing."
)


async def main() -> None:
    uwazi_api = UwaziApiAdapter(user=UWAZI_USER, password=UWAZI_PASSWORD, url=UWAZI_URL)
    llm = OpenRouterAdapter(api_key=OPENROUTER_API_KEY)
    use_case = RunAgentUseCase(llm=llm, api=uwazi_api)

    print("Sending task to OpenRouter via RunAgentUseCase...\n")
    output = await use_case.execute(task_description=TASK_DESCRIPTION, context="")
    print("=== Agent output ===")
    print(output)


if __name__ == "__main__":
    asyncio.run(main())
