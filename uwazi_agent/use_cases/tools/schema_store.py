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
        if self.templates:
            parts.append("Template structures (properties, types, constraints):")
            for template in self.templates.values():
                parts.append(f"  Template '{template.name}':")
                for prop in template.properties:
                    flags = []
                    if prop.required:
                        flags.append("required")
                    if prop.use_as_filter:
                        flags.append("filter")
                    if prop.show_in_card:
                        flags.append("card")
                    flag_str = f" [{', '.join(flags)}]" if flags else ""
                    extra = ""
                    if prop.thesaurus_name:
                        extra += f", thesaurus={prop.thesaurus_name}"
                    if prop.format_instructions:
                        extra += f", format={prop.format_instructions}"
                    if prop.relationship_type_name:
                        extra += f", rel_type={prop.relationship_type_name}"
                    if prop.related_template_name:
                        extra += f", rel_template={prop.related_template_name}"
                    parts.append(f"    - {prop.name}: {prop.type.value}{flag_str}{extra}")
        elif self.template_names:
            parts.append(f"Available templates: {', '.join(self.template_names)}")
        if self.thesauri:
            parts.append("Thesauri values:")
            for thesaurus in self.thesauri.values():
                values_str = ", ".join(thesaurus.values) if thesaurus.values else "none"
                parts.append(f"  '{thesaurus.name}': [{values_str}]")
                for group in thesaurus.groups:
                    group_values = ", ".join(group.values) if group.values else "none"
                    parts.append(f"    group '{group.name}': [{group_values}]")
        elif self.thesauri_names:
            parts.append(f"Available thesauri: {', '.join(self.thesauri_names)}")
        return "\n".join(parts)

    def clear_templates(self) -> None:
        self.template_names.clear()
        self.templates.clear()

    def clear_thesauri(self) -> None:
        self.thesauri_names.clear()
        self.thesauri.clear()

    def clear(self) -> None:
        self.clear_templates()
        self.clear_thesauri()
