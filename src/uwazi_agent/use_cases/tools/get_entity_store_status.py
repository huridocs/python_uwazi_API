from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_entity_store_status(ctx: RunContext[UwaziAgentToolsDependencies]) -> str:
    """Return the current status of the session entity store.

    Use this before processing entities to decide whether the task should be
    delegated to the Python agent (for large batches) or handled directly.

    Returns:
        A string describing how many entities are stored and whether the
        Python agent is required.
    """
    store = ctx.deps.entity_store
    count = len(store.entities)
    if count == 0:
        return "Entity store is empty."
    if store.needs_python_agent:
        return (
            f"Entity store contains {count} entities. "
            f"This exceeds the LLM limit of 5. "
            f"You MUST delegate to the Python agent for batch processing."
        )
    return f"Entity store contains {count} entities. Safe for direct processing."
