"""Isolated unit tests for the language split of recorded values (per AGENTS.md).

A per-language property (text, richtext) is stored by Uwazi as a list of
``{"value", "language"}`` entries, so a translation task records ONE property row
whose value carries several languages. The review table has to show one row per
language, which is what these pure helpers do: ``per_language_split`` is what
the recorder expands with, ``value_in_language`` is how a row compares itself
against the entity's current value, and ``records_to_suggestions`` is the
end-to-end statement of the rule.

No mocks, no I/O, literal inputs only.
"""

from __future__ import annotations

from uwazi_admin_agent.domain.suggestion_scoring import (
    per_language_split,
    records_to_suggestions,
    value_in_language,
    value_language,
    value_languages,
)

TRANSLATION_VALUE = [
    {"value": "hola", "language": "es"},
    {"value": "hello", "language": "en"},
]


def test_value_languages_reads_the_uwazi_value_shape() -> None:
    assert value_languages(TRANSLATION_VALUE) == ["es", "en"]


def test_value_languages_is_empty_for_a_language_independent_value() -> None:
    """A select/multiselect/link/date value has no language — that is a fact, not 'unknown'."""
    assert value_languages("Open") == []
    assert value_languages(["A", "B"]) == []
    assert value_languages(None) == []


def test_value_languages_ignores_entries_without_one() -> None:
    assert value_languages([{"value": "x"}, {"value": "y", "language": "en"}]) == ["en"]


def test_value_language_reports_only_an_unambiguous_language() -> None:
    assert value_language([{"value": "x", "language": "es"}]) == "es"
    assert value_language(TRANSLATION_VALUE) is None
    assert value_language("Open") is None


# --- per_language_split ------------------------------------------------------


def test_per_language_split_gives_one_row_per_language() -> None:
    """The translation task's whole point: two checkable rows, not one opaque one."""
    assert per_language_split(TRANSLATION_VALUE) == [
        ("es", [{"value": "hola", "language": "es"}]),
        ("en", [{"value": "hello", "language": "en"}]),
    ]


def test_per_language_split_keeps_a_language_independent_value_in_one_row() -> None:
    assert per_language_split("Open") == [(None, "Open")]


# --- value_in_language -------------------------------------------------------


def test_value_in_language_narrows_a_multi_language_current_value() -> None:
    """Without this, every translated row would compare itself against its siblings."""
    assert value_in_language(TRANSLATION_VALUE, "es") == [{"value": "hola", "language": "es"}]


def test_value_in_language_is_none_for_a_language_the_entity_lacks() -> None:
    """A translation into a new language is 'new', not a mismatch against everything else."""
    assert value_in_language(TRANSLATION_VALUE, "de") is None


def test_value_in_language_passes_a_language_independent_value_through() -> None:
    assert value_in_language("Open", None) == "Open"
    assert value_in_language(TRANSLATION_VALUE, None) == TRANSLATION_VALUE


def test_value_in_language_reads_an_untagged_value_as_the_rows_own() -> None:
    """Uwazi stores one value per entity-language ROW, with no ``language`` tag.

    The ``locale`` cookie already picked the row, so the whole list IS this
    language's value. Narrowing it against a tag no entry carries would report
    every real text row as "new" instead of comparing it.
    """
    assert value_in_language([{"value": "A/HRC/37/NGO/9 ES"}], "es") == [{"value": "A/HRC/37/NGO/9 ES"}]
    assert value_in_language(["A/HRC/37/NGO/9 ES"], "es") == ["A/HRC/37/NGO/9 ES"]


# --- records_to_suggestions --------------------------------------------------


def test_a_translation_record_becomes_one_row_per_language() -> None:
    rows = records_to_suggestions("run1", [{"op": "update", "shared_id": "s1", "metadata": {"excerpt": TRANSLATION_VALUE}}])

    assert [(row.language, row.property_name) for row in rows] == [("es", "excerpt"), ("en", "excerpt")]
    assert [row.proposed_value for row in rows] == [
        [{"value": "hola", "language": "es"}],
        [{"value": "hello", "language": "en"}],
    ]


def test_a_select_update_stays_one_language_independent_row() -> None:
    rows = records_to_suggestions("run1", [{"op": "update", "shared_id": "s1", "metadata": {"case_status": "Closed"}}])

    assert [(row.language, row.property_name) for row in rows] == [(None, "case_status")]


def test_a_title_row_is_language_independent() -> None:
    rows = records_to_suggestions("run1", [{"op": "update", "shared_id": "s1", "title": "New title"}])

    assert [(row.language, row.property_name) for row in rows] == [(None, "_title")]


def test_a_delete_record_is_language_independent() -> None:
    rows = records_to_suggestions("run1", [{"op": "delete", "shared_id": "s1"}])

    assert [(row.language, row.property_name) for row in rows] == [(None, None)]
