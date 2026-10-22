"""Isolated unit tests for the property language-scope rule (per AGENTS.md).

Pure: literal property types in, a boolean/label out. No mocks, no I/O, no
running instance. The rule is what stops the prompt-clarification agent from
asking "which language row should receive that change?" about a multiselect —
a question the data model already answers.
"""

from uwazi_admin_agent.domain.property_language_scope import (
    LANGUAGE_INDEPENDENT_PROPERTY_TYPES,
    PER_LANGUAGE_PROPERTY_TYPES,
    is_language_independent_property_type,
    is_per_language_property_type,
    language_scope_label,
    language_scope_rule,
)

# --- which types are per-language ------------------------------------------------


def test_the_free_text_types_are_per_language() -> None:
    assert is_per_language_property_type("text") is True
    assert is_per_language_property_type("richtext") is True


def test_a_multiselect_is_language_independent() -> None:
    """A multiselect holds ONE value shared by every language row."""
    assert is_per_language_property_type("multiselect") is False
    assert is_language_independent_property_type("multiselect") is True


def test_the_other_value_types_are_language_independent() -> None:
    for property_type in ("select", "date", "daterange", "multidate", "numeric", "link", "geolocation", "relationship"):
        assert is_language_independent_property_type(property_type) is True, property_type


def test_a_template_only_type_is_language_independent() -> None:
    """``preview``/``nested`` carry no per-entity value at all."""
    assert is_language_independent_property_type("preview") is True
    assert is_language_independent_property_type("nested") is True


def test_an_unknown_type_reads_as_language_independent() -> None:
    """An unknown type is not a reason to ask the operator for a language."""
    assert is_per_language_property_type("something_new") is False
    assert is_language_independent_property_type("something_new") is True


def test_the_type_match_ignores_case_and_padding() -> None:
    assert is_per_language_property_type("  TEXT ") is True
    assert is_language_independent_property_type(" MULTISELECT ") is True


# --- the two sets cover the types -----------------------------------------------


def test_the_per_language_set_holds_only_the_free_text_types() -> None:
    assert PER_LANGUAGE_PROPERTY_TYPES == {"text", "richtext"}


def test_the_two_sets_do_not_overlap() -> None:
    assert not PER_LANGUAGE_PROPERTY_TYPES & LANGUAGE_INDEPENDENT_PROPERTY_TYPES


# --- the rendering ---------------------------------------------------------------


def test_the_scope_label_names_the_scope() -> None:
    assert language_scope_label("text") == "per-language"
    assert language_scope_label("multiselect") == "all languages"


def test_the_rule_block_names_both_halves() -> None:
    rule = language_scope_rule()

    assert "text" in rule
    assert "richtext" in rule
    assert "multiselect" in rule
    assert "NEVER ask" in rule
