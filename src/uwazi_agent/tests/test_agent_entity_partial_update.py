"""Isolated unit tests for the AgentEntity partial-update contract.

Per ``AGENTS.md``: pure models, literal inputs, plain assertions - no mocks,
no network. Regression tests for the bulk-extraction failure where
``update_entities`` dicts built as ``{"shared_id", "template_name", "metadata"}`
(without ``title``) failed ``AgentEntity`` validation with
``ValidationError: title Field required``.
"""

from uwazi_agent.domain.agent_entity import AgentEntity


def test_metadata_only_update_dict_validates_without_title() -> None:
    """The canonical extraction-script update dict has no `title`; it must
    validate (title is optional on partial updates)."""
    entity = AgentEntity(
        **{
            "shared_id": "we2zxthe51",
            "template_name": "DOCUMENT",
            "metadata": {"date_published": "2006-10-02"},
        }
    )
    assert entity.shared_id == "we2zxthe51"
    assert entity.template_name == "DOCUMENT"
    assert entity.metadata == {"date_published": "2006-10-02"}
    assert entity.title is None


def test_title_still_accepted_when_given() -> None:
    entity = AgentEntity(shared_id="a", title="Renamed", template_name="T")
    assert entity.title == "Renamed"


def test_read_shape_roundtrip_preserves_title() -> None:
    """`model_dump` of a fetched entity (which always carries a title) re-validates."""
    read = AgentEntity(shared_id="a", title="t", template_name="T", metadata={})
    again = AgentEntity(**read.model_dump())
    assert again.title == "t"


def test_update_with_title_renames_and_without_preserves() -> None:
    """The repository contract this optionality backs: a None title lets
    `update_partially` keep the stored title; a set title overwrites it."""
    stored_title = "The right of everyone ..."
    entity_no_title = AgentEntity(shared_id="s", template_name="T", metadata={})
    entity_renamed = AgentEntity(shared_id="s", title="New", template_name="T", metadata={})

    effective_no_title = entity_no_title.title if entity_no_title.title is not None else stored_title
    effective_renamed = entity_renamed.title if entity_renamed.title is not None else stored_title

    assert effective_no_title == stored_title
    assert effective_renamed == "New"
