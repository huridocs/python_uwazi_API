from pydantic import BaseModel, Field


class GeneratedScript(BaseModel):
    """The ONE script a generation turn emits.

    The script is a deterministic planning pass: it discovers the affected
    entities (read-only) and emits the intended action per entity through the
    write helpers. It NEVER runs in a namespace whose writers reach Uwazi —
    every run records, and the recorded rows ARE the review table. There is no
    apply script: the table rows are precomputed and executed by the operator's
    Run heuristic (a per-row Uwazi write under the backup intercept), not by a
    second script.

    The script MAY call ``llm_complete`` / ``llm_map`` for values pure Python
    cannot compute — those calls run only under the budget the operator
    approved at task creation (the wizard's caps). The system prompt instructs
    the generator to avoid the LLM tools whenever pure logic suffices, so the
    caps are a ceiling, not a quota.
    """

    python_code: str = Field(
        min_length=1,
        description="The script body: discovery + ONE recorded intended change per entity.",
    )
    description: str | None = Field(default=None, description="Short human-readable intent of the script.")
