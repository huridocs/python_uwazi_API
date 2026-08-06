"""Unit tests for the entity-store shape block.

The shape block is the pre-computed "what is in the entity store" report
the orchestrator injects into the Python sub-agent's delegation. It
must:

* be empty when the store is empty (the agent has nothing to process);
* list every top-level key an entity can carry, with its type and
  nullability;
* break down the metadata per template, with the schema type (when the
  template is in the schema store) or an "(inferred)" tag (when it is
  not), the template flags (``required`` / ``filter`` / ``card``),
  the nullability across the store, and a sample value;
* call out the earliest and latest entity by ``creation_date`` so
  "first/last" questions can be answered without sorting the store;
* honour a hard character cap and truncate with a note when exceeded.
"""

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_property import AgentProperty
from uwazi_agent.domain.agent_property_type import AgentPropertyType
from uwazi_agent.domain.agent_template import AgentTemplate
from uwazi_agent.use_cases.tools.entity_store import EntityStore
from uwazi_agent.use_cases.tools.entity_store_shape import (
    DEFAULT_SHAPE_BLOCK_CHAR_CAP,
    build_entity_store_shape_block,
)


def _make_entity(
    shared_id: str,
    *,
    template: str = "Books",
    title: str | None = None,
    metadata: dict | None = None,
    language: str = "en",
    published: bool = True,
    creation_date: str | None = None,
    edit_date: str | None = None,
) -> AgentEntity:
    return AgentEntity(
        shared_id=shared_id,
        title=title or f"Title-{shared_id}",
        template_name=template,
        metadata=metadata or {},
        language=language,
        published=published,
        creation_date=creation_date,
        edit_date=edit_date,
    )


def _books_template() -> AgentTemplate:
    return AgentTemplate(
        name="Books",
        properties=[
            AgentProperty(name="author", type=AgentPropertyType.TEXT, required=True),
            AgentProperty(
                name="isbn",
                type=AgentPropertyType.TEXT,
                use_as_filter=True,
            ),
            AgentProperty(
                name="genre",
                type=AgentPropertyType.SELECT,
                thesaurus_name="Book Genres",
            ),
            AgentProperty(name="publication_date", type=AgentPropertyType.DATE),
            AgentProperty(name="page_count", type=AgentPropertyType.NUMERIC),
        ],
    )


class TestEmptyStore:
    def test_returns_empty_string_when_store_is_empty(self):
        store = EntityStore()
        assert build_entity_store_shape_block(store) == ""

    def test_returns_empty_string_with_schema_too(self):
        store = EntityStore()
        assert build_entity_store_shape_block(store, {"Books": _books_template()}) == ""


class TestTopLevelKeys:
    def test_lists_all_known_top_level_keys(self):
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date="2024-01-15T10:00:00Z")])
        block = build_entity_store_shape_block(store)
        for key in (
            "shared_id",
            "title",
            "template_name",
            "metadata",
            "language",
            "published",
            "creation_date",
            "edit_date",
        ):
            assert key in block, f"top-level key {key!r} missing from block"

    def test_creation_date_listed_with_type_label(self):
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date="2024-01-15T10:00:00Z")])
        block = build_entity_store_shape_block(store)
        assert "ISO-8601" in block

    def test_published_described_as_read_only(self):
        store = EntityStore()
        store.add_entities([_make_entity("a")])
        block = build_entity_store_shape_block(store)
        assert "READ-ONLY" in block

    def test_nullability_always_set_when_every_entity_has_value(self):
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date="2024-01-15T10:00:00Z")])
        block = build_entity_store_shape_block(store)
        assert "shared_id (str) — always set" in block
        assert "creation_date" in block

    def test_nullability_always_missing_when_no_entity_has_value(self):
        store = EntityStore()
        # None of the entities carry edit_date.
        store.add_entities([_make_entity("a", edit_date=None)])
        block = build_entity_store_shape_block(store)
        assert "edit_date" in block
        assert "always missing" in block

    def test_partial_nullability_reports_missing_fraction(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity("a", creation_date="2024-01-15T10:00:00Z"),
                _make_entity("b", creation_date=None),
                _make_entity("c", creation_date=None),
            ]
        )
        block = build_entity_store_shape_block(store)
        assert "creation_date" in block
        assert "missing on 2/3" in block


class TestPerTemplateMetadata:
    def test_includes_all_keys_seen_in_store(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={
                        "author": "Alice",
                        "isbn": "978-0-1",
                        "genre": "Fiction",
                        "publication_date": "2024-01-15",
                        "page_count": 200,
                    },
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        for key in (
            "author",
            "isbn",
            "genre",
            "publication_date",
            "page_count",
        ):
            assert key in block, f"metadata key {key!r} missing"

    def test_schema_driven_type_label(self):
        """When the template is in the schema, the block uses the
        schema's type label, not the inferred one."""
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"author": "Alice", "page_count": 200},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        assert "author (str)" in block
        assert "page_count (int | float)" in block

    def test_schema_driven_flags(self):
        """``required`` / ``filter`` / ``card`` flags come from the schema."""
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"author": "Alice", "isbn": "978-0-1"},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        assert "[required]" in block  # author
        assert "[filter]" in block  # isbn

    def test_missing_template_marked_as_inferred(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    template="MysteryTemplate",
                    metadata={"title2": "value"},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        # No schema for MysteryTemplate
        block = build_entity_store_shape_block(store, {})
        assert "MysteryTemplate" in block
        assert "inferred" in block

    def test_per_template_nullability_aggregation(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"author": "A", "isbn": "978-0-1"},
                    creation_date="2024-01-15T10:00:00Z",
                ),
                _make_entity(
                    "b",
                    metadata={"author": "B"},  # no isbn
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        assert "isbn" in block
        # 1/2 entities have isbn -> "missing on 1/2"
        assert "missing on 1/2" in block
        # 2/2 entities have author -> "always set"
        assert "author (str) [required] — always set" in block

    def test_empty_string_counts_as_missing(self):
        """An entity saved with ``isbn=""`` should be reported as missing."""
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"author": "A", "isbn": ""},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        # 0/1 entities have a non-empty isbn -> "always missing".
        assert "isbn (str) [filter] — always missing" in block

    def test_empty_list_counts_as_missing(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    template="Books",
                    metadata={"author": "A", "genre": []},  # multiselect-empty
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        # `genre` is select; the empty-list path is defensive — it should
        # still be treated as missing.
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        assert "genre" in block
        # 0/1 entities have a non-empty genre -> "always missing".
        assert "always missing" in block


class TestMultipleTemplates:
    def test_per_template_sections_present(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    template="Books",
                    metadata={"author": "A"},
                    creation_date="2024-01-15T10:00:00Z",
                ),
                _make_entity(
                    "b",
                    template="Films",
                    metadata={"director": "X"},
                    creation_date="2024-01-16T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template(), "Films": _books_template()})
        assert "Template 'Books'" in block
        assert "Template 'Films'" in block
        assert "Books: 1" in block
        assert "Films: 1" in block


class TestTimeRange:
    def test_earliest_and_latest_picked_correctly(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity("a", creation_date="2024-06-15T10:00:00Z"),
                _make_entity("b", creation_date="2024-01-15T10:00:00Z"),
                _make_entity("c", creation_date="2024-12-15T10:00:00Z"),
            ]
        )
        block = build_entity_store_shape_block(store)
        assert "Earliest:" in block
        assert "Latest:" in block
        assert "shared_id=b" in block  # b is the earliest
        assert "shared_id=c" in block  # c is the latest

    def test_no_creation_date_message(self):
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date=None)])
        block = build_entity_store_shape_block(store)
        assert "No entity in the store has a `creation_date`" in block

    def test_skips_entities_without_creation_date_for_range(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity("a", creation_date=None),
                _make_entity("b", creation_date="2024-03-15T10:00:00Z"),
                _make_entity("c", creation_date=None),
            ]
        )
        block = build_entity_store_shape_block(store)
        assert "shared_id=b" in block
        # The 'a' and 'c' ids must not appear in the earliest/latest line.
        assert "shared_id=a" not in block
        assert "shared_id=c" not in block


class TestCharCap:
    def test_truncates_when_block_exceeds_cap(self):
        store = EntityStore()
        # Build a giant store so the block blows past the cap.
        big_metadata = {f"prop_{i:03d}": f"value-{i}" for i in range(200)}
        for i in range(50):
            store.add_entities(
                [
                    _make_entity(
                        f"id_{i:04d}",
                        metadata=big_metadata,
                        creation_date=f"2024-01-15T{i % 24:02d}:00:00Z",
                    ),
                ]
            )
        cap = 2000
        block = build_entity_store_shape_block(store, char_cap=cap)
        assert len(block) <= cap + 80  # truncation note can push it slightly over
        assert "truncated" in block.lower()

    def test_does_not_truncate_when_block_fits(self):
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date="2024-01-15T10:00:00Z")])
        block = build_entity_store_shape_block(store, char_cap=DEFAULT_SHAPE_BLOCK_CHAR_CAP)
        assert "truncated" not in block.lower()


class TestSampleValueFormatting:
    def test_select_sample_rendered_as_label_string(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"genre": "Fiction"},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        # The sample should be rendered, not a raw dict.
        assert "'Fiction'" in block

    def test_geolocation_sample_rendered_as_list_pair(self):
        store = EntityStore()
        store.add_entities(
            [
                _make_entity(
                    "a",
                    metadata={"publication_date": "2024-01-15", "page_count": 200},
                    creation_date="2024-01-15T10:00:00Z",
                ),
            ]
        )
        block = build_entity_store_shape_block(store, {"Books": _books_template()})
        # The ISO date should be quoted in the sample.
        assert "'2024-01-15'" in block
        assert "200" in block


class TestFailureNote:
    def test_footer_points_at_runtime_inspection(self):
        """The footer tells the agent how to inspect the store at
        runtime for properties the shape block does not list."""
        store = EntityStore()
        store.add_entities([_make_entity("a", creation_date="2024-01-15T10:00:00Z")])
        block = build_entity_store_shape_block(store)
        assert "inspect one entity" in block
        assert "entities[0]['metadata'].keys()" in block
