from typing import Any

from pydantic import BaseModel, Field

from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.domain.agent_thesauri import AgentThesauri
from uwazi_api.domain.language import Language


class TemplateEntry(BaseModel):
    """Lightweight per-template snapshot rendered into the system prompt
    so the agent can see template names + entity counts without a tool call.
    """

    name: str
    count: int = 0


class SchemaStore(BaseModel):
    template_names: list[str] = Field(default_factory=list)
    templates: dict[str, AgentTemplate] = Field(default_factory=dict)
    # ``template_entries`` is a richer mirror of ``template_names`` that carries
    # the per-template entity count (when the stats endpoint is available).
    # It is the source of truth for the prompt-context block; ``template_names``
    # is kept around because several tools still read it directly.
    template_entries: list[TemplateEntry] = Field(default_factory=list)
    thesauri_names: list[str] = Field(default_factory=list)
    thesauri: dict[str, AgentThesauri] = Field(default_factory=dict)
    languages: list[Language] = Field(default_factory=list)
    relationship_type_names: list[str] = Field(default_factory=list)
    # Page-builder data. Populated once at boot from the BlockRegistry +
    # VibeRegistry on disk and injected into the page agent's prompt so
    # the LLM does not need tool calls to learn the available block
    # library or visual themes. Intentionally NOT included in
    # to_prompt_context() — see to_page_prompt_context() for the
    # page-agent-specific view that combines both.
    page_blocks: list[dict[str, Any]] = Field(default_factory=list)
    page_vibes: list[str] = Field(default_factory=list)
    page_default_vibe: str = "warm"

    def add_template_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.template_names:
                self.template_names.append(name)
            if not any(entry.name == name for entry in self.template_entries):
                self.template_entries.append(TemplateEntry(name=name, count=0))

    def add_templates(self, templates: list[AgentTemplate]) -> None:
        for template in templates:
            self.templates[template.name] = template
            if template.name not in self.template_names:
                self.template_names.append(template.name)
            if not any(entry.name == template.name for entry in self.template_entries):
                self.template_entries.append(TemplateEntry(name=template.name, count=0))

    def set_template_entries(self, entries: list[TemplateEntry]) -> None:
        """Replace the cached template entries (name + entity count) with a
        fresh snapshot, e.g. after calling the stats endpoint. Unknown names
        that already exist in ``templates``/``template_names`` are preserved
        with count=0 so we never lose a name."""
        seen: set[str] = set()
        merged: list[TemplateEntry] = []
        for entry in entries:
            merged.append(entry)
            seen.add(entry.name)
        for existing in self.template_entries:
            if existing.name not in seen:
                merged.append(TemplateEntry(name=existing.name, count=0))
                seen.add(existing.name)
        for name in self.template_names:
            if name not in seen:
                merged.append(TemplateEntry(name=name, count=0))
                seen.add(name)
        self.template_entries = merged

    def add_thesauri_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.thesauri_names:
                self.thesauri_names.append(name)

    def add_thesauri(self, thesauri: list[AgentThesauri]) -> None:
        for thesaurus in thesauri:
            self.thesauri[thesaurus.name] = thesaurus
            if thesaurus.name not in self.thesauri_names:
                self.thesauri_names.append(thesaurus.name)

    def set_languages(self, languages: list[Language]) -> None:
        self.languages = list(languages)

    def set_relationship_type_names(self, names: list[str]) -> None:
        self.relationship_type_names = list(names)

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
                    extra = f", KEY={prop.name}"
                    if prop.thesaurus_name:
                        extra += (
                            f", thesaurus={prop.thesaurus_name} "
                            "(KEY is the property name, NOT the thesaurus name; "
                            "use a thesaurus LABEL as the value, e.g. 'Fantasy')"
                        )
                    if prop.format_instructions:
                        extra += f", format={prop.format_instructions}"
                    if prop.relationship_type_name:
                        extra += f", rel_type={prop.relationship_type_name}"
                    if prop.related_template_name:
                        extra += f", rel_template={prop.related_template_name}"
                    parts.append(f"    - {prop.name}: {prop.type.value}{flag_str}{extra}")
        elif self.template_entries:
            entries_str = ", ".join(
                f"{entry.name} (entities: {entry.count})" if entry.count else entry.name
                for entry in sorted(
                    self.template_entries,
                    key=lambda e: (-e.count, e.name),
                )
            )
            parts.append(f"Available templates: {entries_str}")
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

    def to_available_context(self) -> str:
        """Render a compact "Available context" block listing the four
        lightweight data sets that are pre-loaded at run start. Designed to
        be prepended to the user prompt so the model knows the instance's
        languages, templates (with entity counts), thesauri, and
        relationship types without a tool call.
        """
        lines: list[str] = ["Available context (snapshot at run start):"]

        if self.languages:
            lang_strs = []
            for lang in self.languages:
                marker = " (default)" if lang.default else ""
                label = f"{lang.key} — {lang.label}" if lang.label else lang.key
                lang_strs.append(f"{label}{marker}")
            lines.append(f"- Languages: {', '.join(lang_strs)}")
        else:
            lines.append("- Languages: unknown (settings port not configured)")

        if self.template_entries:
            entries_str = ", ".join(
                f"{entry.name} ({entry.count} entities)" if entry.count else entry.name
                for entry in sorted(
                    self.template_entries,
                    key=lambda e: (-e.count, e.name),
                )
            )
            lines.append(f"- Templates: {entries_str}")
        elif self.template_names:
            lines.append(f"- Templates: {', '.join(sorted(self.template_names))}")
        else:
            lines.append("- Templates: unknown (template port not configured)")

        if self.thesauri_names:
            lines.append(f"- Thesauri: {', '.join(sorted(self.thesauri_names))}")
        else:
            lines.append("- Thesauri: none")

        if self.relationship_type_names:
            lines.append(f"- Relationship types: {', '.join(sorted(self.relationship_type_names))}")
        else:
            lines.append("- Relationship types: none")

        lines.append(
            "If you need schema details (template properties, thesaurus values, "
            "stats per value), use the read tools — they return the live data."
        )
        return "\n".join(lines)

    def set_page_builder(
        self,
        blocks: list[dict[str, Any]],
        vibes: list[str],
        default_vibe: str = "warm",
    ) -> None:
        """Populate the page-builder section (called once at boot).

        ``blocks`` is the serialised output of ``BlockRegistry.list_blocks()``
        (one dict per block: name, description, when_to_use, required_slots,
        optional_slots). ``vibes`` is the list of vibe names from
        ``VibeRegistry``. ``default_vibe`` is the vibe applied when the
        caller does not specify one — keep it consistent with the
        ``DEFAULT_VIBE`` constant in the page-builder renderer.
        """
        self.page_blocks = list(blocks)
        self.page_vibes = list(vibes)
        self.page_default_vibe = default_vibe

    def to_page_prompt_context(self) -> str:
        """Render the prompt context used by the page sub-agent.

        Combines the regular schema context (templates, thesauri — useful
        when a page pulls live data from those entities) with the
        page-builder block library and available vibes. This is the ONLY
        prompt context that exposes the block library; the entity /
        schema / python agents never see it.
        """
        parts: list[str] = []
        schema_text = self.to_prompt_context()
        if schema_text:
            parts.append(schema_text)
        if self.page_blocks:
            parts.append(self._format_page_blocks())
        if self.page_vibes:
            vibes_str = ", ".join(self.page_vibes)
            parts.append(
                "Available page vibes (visual themes): "
                f"{vibes_str}. Default vibe: `{self.page_default_vibe}` — "
                "if you don't pass a `vibe` to `create_page`, this one is used."
            )
        return "\n\n".join(parts)

    def _format_page_blocks(self) -> str:
        """Serialise the page-block library as a compact prompt block."""
        lines: list[str] = ["Page block library (compose pages by ordering these blocks):"]
        for block in self.page_blocks:
            name = block.get("name", "?")
            desc = block.get("description", "")
            when = block.get("when_to_use", "")
            lines.append(f"  - `{name}`: {desc}" if desc else f"  - `{name}`")
            if when:
                lines.append(f"      when_to_use: {when}")
            required = block.get("required_slots", {}) or {}
            optional = block.get("optional_slots", {}) or {}
            if required:
                lines.append(f"      required_slots: {self._format_slot_map(required)}")
            if optional:
                lines.append(f"      optional_slots: {self._format_slot_map(optional)}")
        return "\n".join(lines)

    @staticmethod
    def _format_slot_map(slots: dict[str, Any]) -> str:
        parts: list[str] = []
        for key, schema in slots.items():
            if not isinstance(schema, dict):
                parts.append(f"{key}={schema!r}")
                continue
            type_str = schema.get("type", "?")
            desc = schema.get("description", "")
            extras: list[str] = []
            if "enum" in schema:
                extras.append(f"enum={schema['enum']}")
            if "default" in schema:
                extras.append(f"default={schema['default']!r}")
            if "item_schema" in schema:
                extras.append(f"item_schema={schema['item_schema']}")
            extra_str = f" [{', '.join(extras)}]" if extras else ""
            line = f"{key} ({type_str})"
            if desc:
                line += f" — {desc}"
            if extra_str:
                line += extra_str
            parts.append(line)
        return "; ".join(parts)

    def clear_templates(self) -> None:
        self.template_names.clear()
        self.templates.clear()
        self.template_entries.clear()

    def clear_thesauri(self) -> None:
        self.thesauri_names.clear()
        self.thesauri.clear()

    def clear_languages(self) -> None:
        self.languages.clear()

    def clear_relationship_types(self) -> None:
        self.relationship_type_names.clear()

    def clear(self) -> None:
        self.clear_templates()
        self.clear_thesauri()
        self.clear_languages()
        self.clear_relationship_types()
