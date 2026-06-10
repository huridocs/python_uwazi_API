from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.domain.agent_thesauri import AgentThesauri


class SchemaStore(BaseModel):
    template_names: list[str] = Field(default_factory=list)
    templates: dict[str, AgentTemplate] = Field(default_factory=dict)
    thesauri_names: list[str] = Field(default_factory=list)
    thesauri: dict[str, AgentThesauri] = Field(default_factory=dict)

    def add_template_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.template_names:
                self.template_names.append(name)

    def add_templates(self, templates: list[AgentTemplate]) -> None:
        for template in templates:
            self.templates[template.name] = template
            if template.name not in self.template_names:
                self.template_names.append(template.name)

    def add_thesauri_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.thesauri_names:
                self.thesauri_names.append(name)

    def add_thesauri(self, thesauri: list[AgentThesauri]) -> None:
        for thesaurus in thesauri:
            self.thesauri[thesaurus.name] = thesaurus
            if thesaurus.name not in self.thesauri_names:
                self.thesauri_names.append(thesaurus.name)

    def to_prompt_context(self) -> str:
        parts = []
        if self.template_names:
            parts.append(f"Available templates: {', '.join(self.template_names)}")
        if self.thesauri_names:
            parts.append(f"Available thesauri: {', '.join(self.thesauri_names)}")
        return "\n".join(parts)

    def clear(self) -> None:
        self.template_names.clear()
        self.templates.clear()
        self.thesauri_names.clear()
        self.thesauri.clear()
