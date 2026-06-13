from typing import Literal

from pydantic import BaseModel

MutationErrorCode = Literal[
    "NOT_FOUND",
    "ALREADY_PUBLISHED",
    "NOT_PUBLISHED",
    "PERMISSION_DENIED",
    "RATE_LIMITED",
    "TEMPLATE_MISMATCH",
    "INVALID_LABEL",
    "INTERNAL",
]


class AgentEntityMutationResult(BaseModel):
    shared_id: str
    success: bool
    error: str | None = None
    error_code: MutationErrorCode | None = None
    # Free-form annotation used to describe non-error side effects (e.g.
    # "already_in_target_state" when the publish-status auto-skip
    # short-circuits an id). Not an error: ``success`` is still ``True``
    # when ``note`` is set.
    note: str | None = None
