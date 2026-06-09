from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def delete_entities_by_shared_ids(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
) -> list[AgentEntityMutationResult]:
    """Delete one or more entities by their ``shared_id``.

    Deletions are **irreversible** and will remove the entity along with
    its documents and attachments. Always confirm with the user before
    calling this tool.

    Identification:
        * Use ``shared_id`` only. Titles are not unique and not safe to
          pass.
        * If you only know an entity by name or partial content, call
          ``search_entities_by_text`` first to discover the
          ``shared_id``, and surface the candidate ids to the user for
          confirmation.

    Partial failures are reported per id; one bad id does not abort the
    others. If a batch delete is rejected (e.g. the ids span multiple
    templates with different permissions), the tool falls back to
    per-id deletion so the rest can still succeed.

    Args:
        shared_ids: The list of Uwazi shared ids to delete.

    Returns:
        A per-entity result indicating success or a descriptive error
        (e.g. unknown shared_id, permission denied).
    """
    if ctx.deps.entity_api is None:
        raise RuntimeError("Entity tools are not configured: `entity_api` is missing on dependencies.")
    return await ctx.deps.entity_api.delete_entities_by_shared_ids(shared_ids=shared_ids)
