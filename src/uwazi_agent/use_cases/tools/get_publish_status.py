from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_publish_status(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    language: str = "en",
) -> list[AgentPublishStatus] | str:
    """Return the current publish status for one or more entities.

    Use this as a pre-flight check before calling ``set_entities_publish_status``
    to (a) drop entities that are already in the target state and (b) detect
    per-entity permission issues before issuing a batch publish. Also use it
    after a publish call to verify the result for individual entities.

    Identification:
        * Pass ``shared_id`` only (titles are not unique).

    Args:
        shared_ids: The entities to inspect, by ``shared_id``.
        language: ISO 639-1 language code. Defaults to "en".

    Returns:
        A list of ``AgentPublishStatus`` (one per requested id) with
        ``shared_id``, ``published`` (boolean) and the full ``permissions``
        array from Uwazi. On error, returns a string describing the problem.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        return await ctx.deps.entity_api.get_publish_status(shared_ids=shared_ids, language=language)
    except Exception as exc:
        logger.error("get_publish_status FAILED: shared_ids={} error={}", shared_ids, exc)
        return f"Error fetching publish status: {exc}. Please retry or use get_entities_by_shared_ids."
