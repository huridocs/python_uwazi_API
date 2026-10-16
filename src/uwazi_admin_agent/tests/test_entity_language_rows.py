"""Isolated unit tests for the store's per-(entity, language) rows (per AGENTS.md).

Real SQLite on a ``tmp_path``, real domain models, plain assertions. No mocks,
no network. This is the review table's read when a run touches the same entity
in several languages (a translation task): ONE row per entity PER LANGUAGE, one
language-independent group, and a checkbox selection that addresses exactly
those rows.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.adapters.sqlite_suggestion_store import SqliteSuggestionStore
from uwazi_admin_agent.domain.suggestion import Suggestion, SuggestionFilter, SuggestionStatus

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store(tmp_path) -> SqliteSuggestionStore:
    db = SqliteSuggestionStore(tmp_path / "suggestions.db")
    yield db
    db.close()


def _suggestion(
    *,
    shared_id: str = "s1",
    language: str | None = None,
    op: str = "update",
    property_name: str | None = "excerpt",
    status: SuggestionStatus = SuggestionStatus.PENDING,
    proposed: object = "v",
    template: str | None = "Judgment",
) -> Suggestion:
    return Suggestion(
        run_id="run1",
        attempt=1,
        shared_id=shared_id,
        entity_title="Title",
        template_name=template,
        op=op,  # type: ignore[arg-type]
        language=language,
        property_name=property_name,
        proposed_value=proposed,
        status=status,
        created_at=NOW,
    )


def _translation(store: SqliteSuggestionStore, shared_id: str = "s1") -> None:
    """A translation task: one entity, one property, two languages."""
    store.add_suggestions(
        [
            _suggestion(shared_id=shared_id, language="es", proposed=[{"value": "hola", "language": "es"}]),
            _suggestion(shared_id=shared_id, language="en", proposed=[{"value": "hello", "language": "en"}]),
        ]
    )


# --- one row per (entity, language) -------------------------------------------


def test_a_translation_is_two_rows_of_the_same_entity(store: SqliteSuggestionStore) -> None:
    _translation(store)

    rows, total = store.entity_rows("run1", 1, SuggestionFilter())

    assert total == 2
    assert [(row.shared_id, row.language) for row in rows] == [("s1", "es"), ("s1", "en")]


def test_a_language_independent_change_is_its_own_row(store: SqliteSuggestionStore) -> None:
    """The select rides beside the translated text without being copied into it."""
    _translation(store)
    store.add_suggestions([_suggestion(shared_id="s1", language=None, property_name="case_status", proposed="Closed")])

    rows, _total = store.entity_rows("run1", 1, SuggestionFilter())

    agnostic = [row for row in rows if row.language is None]
    assert len(agnostic) == 1
    assert agnostic[0].properties == ["case_status"]
    # the per-language rows carry only their own property
    assert [row.properties for row in rows if row.language == "es"] == [["excerpt"]]


def test_an_entity_without_a_language_is_one_row(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(property_name="case_status", proposed="Closed"),
            _suggestion(op="delete", property_name=None),
        ]
    )

    rows, total = store.entity_rows("run1", 1, SuggestionFilter())

    assert total == 1
    assert rows[0].language is None
    assert rows[0].ops == ["update", "delete"]


def test_two_languages_keep_separate_templates(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(language="es", template="Judgment"),
            _suggestion(language="en", template="Report"),
        ]
    )

    rows, _total = store.entity_rows("run1", 1, SuggestionFilter())

    assert {row.language: row.template_names for row in rows} == {"es": ["Judgment"], "en": ["Report"]}


def test_counters_are_counted_per_language_group(store: SqliteSuggestionStore) -> None:
    store.add_suggestions(
        [
            _suggestion(language="es", property_name="excerpt"),
            _suggestion(language="es", property_name="summary"),
            _suggestion(language="en", property_name="excerpt", status=SuggestionStatus.APPLIED),
        ]
    )

    rows, _total = store.entity_rows("run1", 1, SuggestionFilter())
    by_language = {row.language: row for row in rows}

    assert (by_language["es"].proposal_count, by_language["es"].pending, by_language["es"].applied) == (2, 2, 0)
    assert (by_language["en"].proposal_count, by_language["en"].pending, by_language["en"].applied) == (1, 0, 1)


def test_paging_counts_language_rows_not_entities(store: SqliteSuggestionStore) -> None:
    _translation(store)
    store.add_suggestions([_suggestion(shared_id="s2", language="es")])

    rows, total = store.entity_rows("run1", 1, SuggestionFilter(limit=2))

    assert total == 3  # s1/es, s1/en, s2/es
    assert len(rows) == 2


# --- per-language reads -------------------------------------------------------


def test_get_entity_reads_one_language_group(store: SqliteSuggestionStore) -> None:
    _translation(store)

    rows = store.get_entity("run1", 1, "s1", "es")

    assert [row.language for row in rows] == ["es"]


def test_get_entity_reads_the_language_independent_group(store: SqliteSuggestionStore) -> None:
    _translation(store)
    store.add_suggestions([_suggestion(shared_id="s1", property_name="case_status")])

    rows = store.get_entity("run1", 1, "s1")

    assert [row.property_name for row in rows] == ["case_status"]


def test_entity_languages_lists_every_row_of_the_table(store: SqliteSuggestionStore) -> None:
    """What the entity window's Previous/Next walks: rows, not entities."""
    _translation(store)
    store.add_suggestions([_suggestion(shared_id="s1", property_name="case_status"), _suggestion(shared_id="s2")])

    assert store.entity_languages("run1", 1, SuggestionFilter()) == [("s1", "es"), ("s1", "en"), ("s1", None), ("s2", None)]


# --- selecting one language ---------------------------------------------------


def test_a_language_selection_matches_only_that_language(store: SqliteSuggestionStore) -> None:
    _translation(store)

    rows, _total = store.entity_rows("run1", 1, SuggestionFilter(entity_languages=[("s1", "es")]))

    assert [row.language for row in rows] == ["es"]


def test_the_language_independent_group_is_selected_with_a_none_language(store: SqliteSuggestionStore) -> None:
    _translation(store)
    store.add_suggestions([_suggestion(shared_id="s1", property_name="case_status")])

    rows, _total = store.entity_rows("run1", 1, SuggestionFilter(entity_languages=[("s1", None)]))

    assert [row.properties for row in rows] == [["case_status"]]


def test_an_empty_language_selection_matches_nothing(store: SqliteSuggestionStore) -> None:
    _translation(store)

    rows, total = store.entity_rows("run1", 1, SuggestionFilter(entity_languages=[]))

    assert rows == []
    assert total == 0


def test_status_can_be_set_for_one_language_group_only(store: SqliteSuggestionStore) -> None:
    _translation(store)

    count = store.set_status_by_filter(
        "run1", 1, SuggestionFilter(entity_languages=[("s1", "es")]), SuggestionStatus.ACCEPTED
    )

    assert count == 1
    assert [row.language for row in store.get_entity("run1", 1, "s1", "es")] == ["es"]
    assert [row.status for row in store.get_entity("run1", 1, "s1", "en")] == [SuggestionStatus.PENDING]
