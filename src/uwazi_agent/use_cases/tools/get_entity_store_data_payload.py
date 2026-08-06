from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


async def get_entity_store_data_payload(
    ctx: RunContext[UwaziAgentToolsDependencies],
    key: str,
) -> object:
    """Read a prepared data payload from the session entity store.

    The orchestrator (often via the Python agent) sometimes prepares data
    (e.g. a list of timeline entries, a chart dataset, a catalog) and
    stashes it under a named key in the entity store. This tool lets you
    fetch it back so you can use it as the source for a page you are
    about to build (e.g. as the ``entries`` slot of a ``timeline`` block
    or the ``cards`` slot of a ``card_grid`` block).

    The data is not re-queried from Uwazi here — it was already gathered
    and processed before you were called, so this read is cheap and
    offline.

    Use ``list_entity_store_data_payload_keys`` to discover what keys
    have been prepared before you ask for one.

    Args:
        key: The name of the payload to read. The orchestrator normally
            communicates the key in the task description (e.g. "the
            timeline entries are in data_payload['timeline_entries']").

    Returns:
        The payload stored under ``key`` (typically a list of dicts or a
        list of strings), or ``None`` if no payload is stored under that
        key. The page agent should not branch on missing data — if the
        payload is ``None``, respond to the orchestrator asking for the
        data to be prepared first.
    """
    if ctx.deps.entity_store.has_data_payload(key):
        return ctx.deps.entity_store.get_data_payload(key)
    return None


async def list_entity_store_data_payload_keys(
    ctx: RunContext[UwaziAgentToolsDependencies],
) -> list[str]:
    """List the data payload keys that have been prepared in the session entity store.

    Use this to discover what prepared data the orchestrator (or a
    previous Python-agent run) has made available. The Python agent is
    the only tool that can produce data payloads — see
    ``delegate_to_python_agent`` in the orchestrator.

    Returns:
        Sorted list of payload keys (may be empty).
    """
    return ctx.deps.entity_store.list_data_payload_keys()
