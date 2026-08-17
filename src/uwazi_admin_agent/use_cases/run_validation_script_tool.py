"""The ``run_validation_script`` tool bound to the script-generation agent.

Phase 2 ships a STUB: the real dummy-entity validation harness (create throwaway
dummies in the real instance, run the candidate script against them, assert the
expected outcome, revert to exact original raw state, then delete the dummies on
success and failure — §2.7) lands in Phase 3 and replaces this body. The stub is
wired now so the agent's tool set and system prompt match the final shape; the
agent still generates a script without a working validator.

The tool is typed against ``UwaziAgentToolsDependencies`` (the agent's
``deps_type``) so it slots into the same ``RunContext`` as ``query_entities``.
Phase 3 will likely extend the deps to carry the validation harness + attempt
counter (mirroring ``browser_agent``'s ``AgentDeps``); that change is deferred.
"""

from __future__ import annotations

from pydantic_ai import RunContext

from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies

_STUB_MESSAGE = (
    "Validation is not available in this build (the dummy-entity harness lands in "
    "Phase 3). Do NOT call this tool again. Finish exploring with `query_entities`, "
    "then emit your best `GeneratedScript` directly."
)


async def run_validation_script(ctx: RunContext[UwaziAgentToolsDependencies], python_code: str) -> str:
    """Validate a candidate script against dummy entities in the real instance.

    The intended behaviour (Phase 3): create throwaway dummy entities matching
    the script's target, run ``python_code`` against them, assert the expected
    outcome AND that revert restores their exact original raw state, then delete
    the dummies (on success and failure). Returns pass/fail + a before/after
    diff + restore-equality evidence so you can repair the script before emitting
    it.

    In THIS build the tool is a stub and always returns "not available" — emit
    your best script without validating.
    """
    _ = ctx, python_code  # unused in the stub; Phase 3 wires the real harness.
    return _STUB_MESSAGE
