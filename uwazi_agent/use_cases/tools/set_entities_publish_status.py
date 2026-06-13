from loguru import logger
from pydantic_ai import RunContext

from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies
from uwazi_api.domain.exceptions import DomainError


async def set_entities_publish_status(
    ctx: RunContext[UwaziAgentToolsDependencies],
    shared_ids: list[str],
    published: bool,
    auto_skip_already_in_target_state: bool = True,
) -> list[AgentEntityMutationResult] | str:
    """Publish or unpublish one or more entities.

    Publishing makes an entity visible to the public (anonymous) audience;
    unpublishing makes it private again (visible only to logged-in users with
    permission). This does not delete anything — it only toggles public
    visibility.

    Identification:
        * Use ``shared_id`` only (titles are not unique). Discover ids with
          ``query_entities`` first if needed.

    Auto-skip (default on):
        * When ``auto_skip_already_in_target_state=True`` (the default), the
          tool inspects each id's current publish state and silently drops
          any id that is already in the target state. The per-id result
          list still contains a synthetic ``success=True`` entry for every
          skipped id (with a ``note`` field describing the skip) so the
          caller's 1:1 mapping between input ids and result entries is
          preserved. The caller does NOT need to call
          ``get_publish_status`` first to implement this pre-flight.
        * Set ``auto_skip_already_in_target_state=False`` if you want the
          underlying publish/unpublish call to be issued for every id
          regardless of current state (useful for retrying after a
          partial failure).

    Args:
        shared_ids: The entities to update, by ``shared_id``.
        published: ``True`` to publish (make public), ``False`` to unpublish
            (make private).
        auto_skip_already_in_target_state: When ``True`` (default), ids
            already in the target state are skipped and reported as
            successful no-ops.

    Returns:
        A per-entity result indicating success or a descriptive error. On
        catastrophic error, returns a string describing the problem.
    """
    if ctx.deps.entity_api is None:
        return "Error: Entity tools are not configured: `entity_api` is missing on dependencies."

    ids_to_call: list[str] = list(shared_ids)
    skipped_results: dict[str, AgentEntityMutationResult] = {}
    if auto_skip_already_in_target_state and ids_to_call:
        try:
            current = await ctx.deps.entity_api.get_publish_status(shared_ids=ids_to_call, language="en")
        except DomainError as exc:
            logger.warning(
                "set_entities_publish_status auto-skip preflight failed; falling back to issuing the call for all ids: {}",
                exc,
            )
            current = []
        current_by_id = {item.shared_id: item for item in current}
        remaining: list[str] = []
        for sid in ids_to_call:
            state = current_by_id.get(sid)
            if state is not None and state.published == published:
                skipped_results[sid] = AgentEntityMutationResult(
                    shared_id=sid,
                    success=True,
                    note="already_in_target_state",
                )
            else:
                remaining.append(sid)
        ids_to_call = remaining
        logger.info(
            "set_entities_publish_status: auto-skip dropped {} ids, {} remain",
            len(skipped_results),
            len(ids_to_call),
        )

    if not ids_to_call:
        return [skipped_results[sid] for sid in shared_ids if sid in skipped_results]

    try:
        results = await ctx.deps.entity_api.set_entities_publish_status(shared_ids=ids_to_call, published=published)
    except DomainError as exc:
        logger.error(
            "set_entities_publish_status FAILED: shared_ids={} published={} error={}",
            ids_to_call,
            published,
            exc,
        )
        action = "publishing" if published else "unpublishing"
        return f"Error {action} entities: {exc}. Please verify the shared_ids and retry."

    # Re-stitch the per-id results into the original input order, filling
    # in synthetic "skipped" entries where applicable.
    by_id = {r.shared_id: r for r in results}
    for r in results:
        by_id.setdefault(r.shared_id, r)
    final: list[AgentEntityMutationResult] = []
    for sid in shared_ids:
        if sid in skipped_results:
            final.append(skipped_results[sid])
        elif sid in by_id:
            final.append(by_id[sid])
        else:
            final.append(
                AgentEntityMutationResult(
                    shared_id=sid,
                    success=False,
                    error="id not returned by adapter",
                )
            )
    return final
