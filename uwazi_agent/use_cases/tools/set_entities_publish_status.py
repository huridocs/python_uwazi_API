from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def set_entities_publish_status(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    published: bool,
) -> list[AgentEntityMutationResult] | str:
    logger.info("set_entities_publish_status(shared_ids={!r}, published={!r})", shared_ids, published)
    """Publish or unpublish one or more entities.

    Publishing makes an entity visible to the public (anonymous) audience;
    unpublishing makes it private again (visible only to logged-in users with
    permission). This does not delete anything — it only toggles public
    visibility.

    Identification:
        * Use ``shared_id`` only (titles are not unique). Discover ids with
          ``search_entities_by_text``, ``search_entities_by_filter``, or
          ``get_entities_by_template`` first if needed.

    Args:
        shared_ids: The entities to update, by ``shared_id``.
        published: ``True`` to publish (make public), ``False`` to unpublish
            (make private).

    Returns:
        A per-entity result indicating success or a descriptive error. On
        catastrophic error, returns a string describing the problem.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."
    try:
        return await ctx.deps.entity_api.set_entities_publish_status(shared_ids=shared_ids, published=published)
    except DomainError as exc:
        logger.error("set_entities_publish_status FAILED: shared_ids={} published={} error={}", shared_ids, published, exc)
        action = "publishing" if published else "unpublishing"
        return f"Error {action} entities: {exc}. Please verify the shared_ids and retry."
