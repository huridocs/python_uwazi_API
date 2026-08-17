from pydantic import BaseModel, Field


class RunConfig(BaseModel):
    """A single named admin run (mirrors ``browser_agent``'s ``RunConfig``).

    ``name`` becomes the per-run folder under the runs root; ``prompt`` is the
    natural-language request the script-generation agent turns into a script.
    Further knobs (target template, language, scope) are added in later phases
    as the use cases need them — do not add them prematurely.
    """

    name: str = Field(min_length=1, description="Unique run name; becomes the folder name under the runs root.")
    prompt: str = Field(min_length=1, description="The operator's natural-language request.")
