import asyncio

from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent import configuration
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def opencode_query(
    ctx: RunContext[UwaziAgentToolsDependencies],
    question: str,
) -> str:
    """Ask a question about a local repository using the ``opencode`` CLI.

    This tool shells out to ``opencode`` (the same binary the ``plan`` agent
    uses) so the LLM can answer questions about a code repository without
    having to load the files itself. It is purely a thin wrapper around
    ``opencode run`` with the ``plan`` agent.

    Args:
        question: The natural-language question to ask about the repository
            configured via ``configuration.UWAZI_REPOSITORY_PATH``.

    Returns:
        The trimmed stdout produced by ``opencode``, or an error message.
    """
    repository_path = configuration.UWAZI_REPOSITORY_PATH
    model = configuration.MODEL

    logger.info(
        "opencode_query: repo={} model={} question={!r}",
        repository_path,
        model,
        question,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            "opencode",
            "run",
            "-m",
            f"ollama-cloud/{model}",
            "--dir",
            str(repository_path),
            "--format",
            "default",
            "--agent",
            "plan",
            question,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        return (
            f"Error: the 'opencode' executable was not found on PATH ({exc}). "
            "Install opencode or adjust the environment before retrying."
        )

    try:
        stdout, stderr = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise

    if proc.returncode != 0:
        error_output = stderr.decode().strip() or stdout.decode().strip()
        logger.error(
            "opencode_query FAILED (returncode={}): {}",
            proc.returncode,
            error_output,
        )
        return f"Error running opencode (returncode {proc.returncode}):\n{error_output}"

    output = stdout.decode().strip()
    limit = configuration.PYTHON_SCRIPT_OUTPUT_CHARACTERS_LIMIT
    if len(output) > limit:
        logger.warning("opencode_query output truncated from {} to {} characters", len(output), limit)
        output = output[:limit] + "\n... [output truncated]"
    return output
