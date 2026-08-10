from pydantic import BaseModel, Field, model_validator


class EntityFilter(BaseModel):
    """How an op selects the entities it applies to.

    At least one criterion must be provided; an empty filter is rejected.
    Criteria are AND-combined when more than one is set.
    """

    shared_ids: list[str] | None = Field(default=None, description="Explicit shared ids to select.")
    template: str | None = Field(default=None, description="Select entities of this template name.")
    search_text: str | None = Field(default=None, description="Free text search to select entities.")
    language: str | None = Field(default=None, description="Select entities in this language code.")

    @model_validator(mode="after")
    def _at_least_one_criterion(self) -> "EntityFilter":
        if self.shared_ids:
            return self
        if self.template is not None and self.template.strip():
            return self
        if self.search_text is not None and self.search_text.strip():
            return self
        if self.language is not None and self.language.strip():
            return self
        raise ValueError(
            "EntityFilter must carry at least one selection criterion (shared_ids, template, search_text, or language)."
        )
