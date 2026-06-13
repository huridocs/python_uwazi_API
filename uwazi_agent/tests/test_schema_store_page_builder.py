"""Unit tests for the schema store's page-builder section.

Verifies the contract that the page sub-agent relies on:

* ``set_page_builder`` populates the three fields (blocks, vibes, default).
* ``to_prompt_context()`` does NOT include the page-builder section —
  other sub-agents (entity, schema, python) must never see it.
* ``to_page_prompt_context()`` does include it, and combines the
  regular schema context with the page-builder section.
"""

from uwazi_agent.use_cases.tools.schema_store import SchemaStore


def _sample_blocks() -> list[dict]:
    return [
        {
            "name": "hero",
            "description": "Opening section",
            "when_to_use": "First block of any page",
            "required_slots": {"title": {"type": "string", "description": "Heading"}},
            "optional_slots": {
                "subtitle": {
                    "type": "string",
                    "description": "Subheading",
                    "default": "",
                },
                "height": {
                    "type": "string",
                    "description": "Banner height",
                    "default": "medium",
                    "enum": ["small", "medium", "large"],
                },
            },
        },
        {
            "name": "timeline",
            "description": "Vertical timeline",
            "when_to_use": "Chronological histories",
            "required_slots": {
                "entries": {
                    "type": "list",
                    "description": "Entries",
                    "item_schema": {
                        "date": {"type": "string", "description": "Date label"},
                        "title": {"type": "string", "description": "Entry title"},
                        "description": {"type": "string", "description": "Body"},
                    },
                }
            },
            "optional_slots": {},
        },
    ]


def _sample_vibes() -> list[str]:
    return ["minimal", "warm", "ocean"]


class TestSchemaStorePageBuilder:
    def test_to_prompt_context_does_not_include_page_builder(self):
        store = SchemaStore()
        store.set_page_builder(_sample_blocks(), _sample_vibes(), "minimal")
        # Even after set_page_builder, to_prompt_context stays empty
        # (no templates / thesauri loaded).
        assert store.to_prompt_context() == ""
        # And explicitly: no page-builder marker should appear.
        assert "Page block library" not in store.to_prompt_context()
        assert "Available page vibes" not in store.to_prompt_context()

    def test_to_page_prompt_context_includes_blocks_and_vibes(self):
        store = SchemaStore()
        store.set_page_builder(_sample_blocks(), _sample_vibes(), "minimal")
        text = store.to_page_prompt_context()
        # Block metadata is present.
        assert "Page block library" in text
        assert "`hero`" in text
        assert "Opening section" in text
        assert "First block of any page" in text
        # Required-slot description shows up.
        assert "Heading" in text
        # Optional-slot enum values each show up (we don't pin the
        # exact separator/repr since Python's default list/str repr
        # is used).
        assert "small" in text
        assert "medium" in text
        assert "large" in text
        assert "enum=" in text
        # Vibe list + default vibe rule.
        assert "Available page vibes" in text
        assert "minimal" in text
        assert "warm" in text
        assert "ocean" in text
        assert "Default vibe: `minimal`" in text

    def test_to_page_prompt_context_is_empty_when_page_builder_not_set(self):
        store = SchemaStore()
        text = store.to_page_prompt_context()
        assert text == ""
        # The page-builder markers must not appear in the empty state.
        assert "Page block library" not in text
        assert "Available page vibes" not in text

    def test_set_page_builder_is_a_snapshot(self):
        store = SchemaStore()
        original = _sample_blocks()
        store.set_page_builder(original, _sample_vibes(), "minimal")
        # Mutating the source list after set_page_builder must not leak in.
        original.append({"name": "intruder"})
        text = store.to_page_prompt_context()
        assert "intruder" not in text

    def test_to_prompt_context_includes_schemas_alongside_page_builder(self):
        """to_page_prompt_context = regular schema + page builder."""
        from uwazi_agent.domain.agent_template import AgentTemplate

        store = SchemaStore()
        store.set_page_builder(_sample_blocks(), _sample_vibes(), "minimal")
        store.add_templates([AgentTemplate(name="Books", properties=[])])
        text = store.to_page_prompt_context()
        # Regular schema part.
        assert "Template structures" in text
        assert "Books" in text
        # Page-builder part.
        assert "Page block library" in text
        assert "Available page vibes" in text


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v", "-s"])
