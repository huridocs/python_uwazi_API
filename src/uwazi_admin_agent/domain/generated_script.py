from typing import Literal

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

    ``plan_kind`` is the operator-facing INTENT declaration and it is enforced
    downstream: a ``changes`` plan that records no change operation (only info
    rows) is a FAILED generation, not a review table — that is the exact shape
    of a script that gave up and reported its failure through ``add_info``.
    ``query`` is only for a prompt that asks for a READ (a value, a list, a
    yes/no answer); its info rows are the deliverable. See
    :class:`~uwazi_admin_agent.use_cases.generate_predictions_use_case.GeneratePredictionsUseCase`.
    """

    plan_kind: Literal["changes", "query"] = Field(
        default="changes",
        description=(
            "'changes' when the prompt asks to WRITE to Uwazi (create/update/delete/publish/merge/"
            "file operations); 'query' ONLY when the prompt asks for a READ answer and nothing may be written. "
            "The default is the strict one: a script that forgot to declare is treated as a change plan."
        ),
    )
    python_code: str = Field(
        min_length=1,
        description="The script body: discovery + ONE recorded intended change per entity (or a read-only answer).",
    )
    description: str | None = Field(default=None, description="Short human-readable intent of the script.")
