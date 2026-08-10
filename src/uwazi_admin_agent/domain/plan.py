from pydantic import BaseModel, model_validator

from uwazi_admin_agent.domain.ops import ExtractFromSupportingFileOp, Op, SetPropertyOp


class MigrationPlan(BaseModel):
    """A declarative migration: a human description plus an ordered list of ops.

    The LLM emits a `MigrationPlan` and nothing else (no code, no free-form instructions).
    Invariants are enforced at construction (§5.1).
    """

    description: str
    ops: list[Op]

    @model_validator(mode="after")
    def _validate_plan(self) -> "MigrationPlan":
        if not self.ops:
            raise ValueError("MigrationPlan must contain at least one op.")

        # Reject any two property writes to the same (filter, property_name):
        # two writes to the same property on the same entities are ordering-
        # dependent and ambiguous, regardless of the overwrite flag.
        seen: set[tuple[str, str]] = set()
        for op in self.ops:
            if isinstance(op, (SetPropertyOp, ExtractFromSupportingFileOp)):
                key = (op.filter.model_dump_json(), op.property_name)
                if key in seen:
                    raise ValueError(
                        f"Conflicting write to property '{op.property_name}' on the same "
                        "filter; merge or remove the duplicate op."
                    )
                seen.add(key)
        return self
