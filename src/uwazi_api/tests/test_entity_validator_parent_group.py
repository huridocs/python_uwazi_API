"""Isolated unit tests for the thesaurus parent-group resolution (per AGENTS.md).

Pure: literal ``parent`` dicts in, a label out. No mocks, no I/O, no running
instance — the resolution is a projection over the thesaurus's own groups.
"""

from uwazi_api.use_cases.repositories.entity_validator import EntityValidator

# The shape ``/api/thesauris`` returns: groups carry an id + a label, and their
# children carry an id + a label. Labels are NOT translated per locale — the same
# uuid reads "United Nations" in every locale.
GROUP_ID = "1c2ea46d-08ba-4380-91b6-4414258bc969"
ID_TO_LABEL = {GROUP_ID: "United Nations"}


def test_a_translated_parent_label_resolves_by_its_group_uuid() -> None:
    """Uwazi translates the parent's LABEL but keeps its uuid.

    This is the stored shape of a Spanish select row: ``parent.label`` reads
    "Naciones Unidas" while the value stays the English group's uuid. Resolving by
    the label alone refused the whole entity, which is why a Spanish
    ``document_ref`` update died on an unrelated ``document_type`` group.
    """
    parent = {"value": GROUP_ID, "label": "Naciones Unidas"}

    assert EntityValidator._parent_group_label(parent, ID_TO_LABEL) == "United Nations"


def test_an_untranslated_parent_resolves_the_same_way() -> None:
    parent = {"value": GROUP_ID, "label": "United Nations"}

    assert EntityValidator._parent_group_label(parent, ID_TO_LABEL) == "United Nations"


def test_a_label_only_parent_falls_back_to_its_label() -> None:
    """The LLM-facing shape carries no group uuid at all."""
    assert EntityValidator._parent_group_label({"label": "United Nations"}, ID_TO_LABEL) == "United Nations"


def test_an_unknown_group_returns_what_it_was_given() -> None:
    """So the caller's "not found in thesaurus" error still fires, naming it."""
    parent = {"value": "not-a-group-uuid", "label": "Naciones Unidas"}

    assert EntityValidator._parent_group_label(parent, ID_TO_LABEL) == "Naciones Unidas"


def test_an_empty_parent_resolves_to_nothing() -> None:
    assert EntityValidator._parent_group_label({}, ID_TO_LABEL) == ""
