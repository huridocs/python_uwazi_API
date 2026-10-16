"""Isolated unit tests for the apply payload grouping (per AGENTS.md).

Pure: literal :class:`Suggestion` lists in, payload dicts out. No mocks, no I/O,
no instance.
"""

import pytest

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionStatus
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    ApplyRefusedError,
    build_apply_creates,
    build_apply_updates,
    effective_value,
    merge_language_value,
    partition_for_apply,
    pushable_rows,
    stamp_language,
)


def _s(
    *,
    shared_id: str = "s1",
    language: str | None = None,
    op: str = "update",
    property_name: str | None = "excerpt",
    status: SuggestionStatus = SuggestionStatus.ACCEPTED,
    proposed: object = "new value",
    edited: str | None = None,
    template_name: str | None = None,
) -> Suggestion:
    return Suggestion(
        run_id="run1",
        attempt=1,
        shared_id=shared_id,
        language=language,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        status=status,
        edited_value=edited,
        template_name=template_name,
    )


# --- pushable_rows -----------------------------------------------------------


def test_pushable_accepts_accepted_and_edited_only() -> None:
    rows = [
        _s(shared_id="a", status=SuggestionStatus.PENDING),
        _s(shared_id="b", status=SuggestionStatus.ACCEPTED),
        _s(shared_id="c", status=SuggestionStatus.EDITED, edited='"x"'),
        _s(shared_id="d", status=SuggestionStatus.DISCARDED),
        _s(shared_id="e", status=SuggestionStatus.APPLIED),
    ]
    assert [r.shared_id for r in pushable_rows(rows)] == ["b", "c"]


def test_pushable_includes_whole_entity_ops_and_skips_propertyless_updates() -> None:
    rows = [
        _s(shared_id="a", op="delete", property_name=None),
        _s(shared_id="b", op="publish", property_name=None),
        _s(shared_id="c", op="set_publish_status", property_name=None),
        _s(shared_id="d", op="create_relationships", property_name=None),
        _s(shared_id="e", op="update", property_name=None),
        _s(shared_id="f", op="update", property_name="topic"),
    ]
    assert [r.shared_id for r in pushable_rows(rows)] == ["a", "b", "c", "d", "f"]


def test_pushable_honors_a_subset() -> None:
    """A Run's selection is (sharedId, language) pairs, not bare ids."""
    rows = [_s(shared_id="a"), _s(shared_id="b"), _s(shared_id="c")]
    assert [r.shared_id for r in pushable_rows(rows, [("a", None), ("c", None)])] == ["a", "c"]
    assert pushable_rows(rows, []) == []


def test_pushable_selection_is_per_language() -> None:
    """Checking one language of an entity pushes that language only."""
    rows = [_s(shared_id="a", language="es"), _s(shared_id="a", language="en"), _s(shared_id="b")]
    assert [r.language for r in pushable_rows(rows, [("a", "es")])] == ["es"]


def test_pushable_includes_create_rows() -> None:
    rows = [_s(shared_id="dry-created-0", op="create", property_name="title_field")]
    assert [r.shared_id for r in pushable_rows(rows)] == ["dry-created-0"]


# --- effective_value ---------------------------------------------------------


def test_edited_row_uses_its_edited_value_with_json_decoding() -> None:
    row = _s(status=SuggestionStatus.EDITED, proposed="original", edited='[{"value": "typed"}]')
    assert effective_value(row) == [{"value": "typed"}]


def test_edited_row_with_plain_text_keeps_the_string() -> None:
    row = _s(status=SuggestionStatus.EDITED, proposed="original", edited="just words")
    assert effective_value(row) == "just words"


def test_accepted_row_uses_its_proposal() -> None:
    assert effective_value(_s(proposed=[{"value": "v"}])) == [{"value": "v"}]


def test_edited_status_without_a_value_falls_back_to_the_proposal() -> None:
    assert effective_value(_s(status=SuggestionStatus.EDITED, proposed="fallback", edited=None)) == "fallback"


# --- build_apply_updates -----------------------------------------------------


def test_groups_rows_by_shared_id_into_one_payload() -> None:
    """Values are applied VERBATIM: the recorder's shapes are what the mapper coerces.

    Re-wrapping a proposal (e.g. into ``[{"value": ...}]``) would hand the entity
    mapper a list where a ``select`` expects a scalar label, and Uwazi rejects it
    — so the reviewed value is passed through unchanged.
    """
    updates = build_apply_updates(
        [
            _s(shared_id="s1", property_name="excerpt", proposed="e"),
            _s(shared_id="s1", property_name="topic", proposed=["t"]),
            _s(shared_id="s2", property_name="excerpt", proposed="other"),
        ]
    )
    assert updates == [
        {"shared_id": "s1", "metadata": {"excerpt": "e", "topic": ["t"]}},
        {"shared_id": "s2", "metadata": {"excerpt": "other"}},
    ]


def test_group_order_follows_first_appearance() -> None:
    updates = build_apply_updates(
        [
            _s(shared_id="z", property_name="a"),
            _s(shared_id="a", property_name="b"),
            _s(shared_id="z", property_name="c"),
        ]
    )
    assert [u["shared_id"] for u in updates] == ["z", "a"]


def test_title_row_becomes_a_top_level_title_field() -> None:
    updates = build_apply_updates(
        [
            _s(shared_id="s1", property_name=TITLE_PROPERTY, proposed="New Title"),
            _s(shared_id="s1", property_name="excerpt", proposed="e"),
        ]
    )
    assert updates == [{"shared_id": "s1", "metadata": {"excerpt": "e"}, "title": "New Title"}]


def test_no_title_key_when_there_is_no_title_row() -> None:
    updates = build_apply_updates([_s(shared_id="s1", property_name="excerpt", proposed="e")])
    assert "title" not in updates[0]


def test_edited_overrides_proposed_for_the_same_key() -> None:
    updates = build_apply_updates(
        [
            _s(shared_id="s1", property_name="excerpt", proposed="proposed"),
            _s(
                shared_id="s1",
                property_name="excerpt",
                proposed="proposed",
                status=SuggestionStatus.EDITED,
                edited='"edited"',
            ),
        ]
    )
    assert updates[0]["metadata"]["excerpt"] == "edited"


def test_subset_restricts_the_payload() -> None:
    rows = [_s(shared_id="a", property_name="x"), _s(shared_id="b", property_name="x")]
    updates = build_apply_updates(rows, [("b", None)])
    assert [u["shared_id"] for u in updates] == ["b"]


# --- per-language payloads ---------------------------------------------------


def test_two_checked_languages_of_one_property_merge_into_one_value_list() -> None:
    """A property holds one value PER LANGUAGE: checking both writes both, not the last."""
    rows = [
        _s(language="es", proposed=[{"value": "hola", "language": "es"}]),
        _s(language="en", proposed=[{"value": "hello", "language": "en"}]),
    ]

    updates = build_apply_updates(rows, [("s1", "es"), ("s1", "en")])

    assert updates[0]["metadata"]["excerpt"] == [
        {"value": "hola", "language": "es"},
        {"value": "hello", "language": "en"},
    ]


def test_one_checked_language_writes_only_that_language() -> None:
    rows = [
        _s(language="es", proposed=[{"value": "hola", "language": "es"}]),
        _s(language="en", proposed=[{"value": "hello", "language": "en"}]),
    ]

    updates = build_apply_updates(rows, [("s1", "es")])

    assert updates[0]["metadata"]["excerpt"] == [{"value": "hola", "language": "es"}]


# --- merge_language_value ----------------------------------------------------


def test_merge_keeps_the_other_languages_already_in_the_entity() -> None:
    """The whole point: writing es must not delete the en already in Uwazi."""
    current = [{"value": "hello", "language": "en"}, {"value": "hola", "language": "es"}]
    proposed = [{"value": "hola nuevo", "language": "es"}]

    assert merge_language_value(current, proposed) == [
        {"value": "hello", "language": "en"},
        {"value": "hola nuevo", "language": "es"},
    ]


def test_merge_adds_the_language_the_entity_did_not_have() -> None:
    assert merge_language_value([{"value": "hello", "language": "en"}], [{"value": "hola", "language": "es"}]) == [
        {"value": "hello", "language": "en"},
        {"value": "hola", "language": "es"},
    ]


def test_merge_replaces_a_language_independent_value_wholesale() -> None:
    """A select is not a language list: Uwazi replaces it whole, which is the proposal's meaning."""
    assert merge_language_value("Open", "Closed") == "Closed"
    assert merge_language_value(None, [{"value": "x", "language": "es"}]) == [{"value": "x", "language": "es"}]


def test_list_values_pass_through_unchanged() -> None:
    payload = [{"value": "t1", "label": "T1"}]
    updates = build_apply_updates([_s(property_name="topic", proposed=payload)])
    assert updates[0]["metadata"]["topic"] == payload


def test_dict_value_passes_through_unchanged() -> None:
    """A link/geolocation dict reaches the mapper in the shape the script contract set."""
    payload = {"label": "Source", "url": "https://example.org"}
    updates = build_apply_updates([_s(property_name="source_page_url", proposed=payload)])
    assert updates[0]["metadata"]["source_page_url"] == payload


def test_none_value_passes_through_so_the_clear_is_explicit() -> None:
    """A reviewed "clear this property" proposal stays None; the write adapter
    turns it into the empty shape Uwazi accepts."""
    updates = build_apply_updates([_s(property_name="excerpt", proposed=None)])
    assert updates[0]["metadata"]["excerpt"] is None


def test_build_apply_updates_skips_everything_unpushable() -> None:
    rows = [
        _s(shared_id="a", status=SuggestionStatus.PENDING),
        _s(shared_id="b", op="delete", property_name=None),
    ]
    # A pending row never pushes and a whole-entity op carries no property row,
    # so the update-payload grouping yields nothing pushable of its own.
    assert build_apply_updates(rows) == []


def test_build_apply_updates_on_empty_input() -> None:
    assert build_apply_updates([]) == []


def test_metadata_key_order_follows_row_order() -> None:
    updates = build_apply_updates(
        [
            _s(shared_id="s1", property_name="zebra"),
            _s(shared_id="s1", property_name="alpha"),
        ]
    )
    assert list(updates[0]["metadata"]) == ["zebra", "alpha"]


# --- create rows are NOT update payloads ---------------------------------------


def test_create_rows_never_become_update_payloads() -> None:
    """A create's sharedId is a ``dry-created-N`` placeholder Uwazi has never seen.

    Enriching an update payload reads that entity's raw to resolve its template
    and its other languages, so a create reaching this grouping aborted the whole
    Run with ``get_raw_by_shared_id(dry-created-0) failed (404)`` before a single
    entity was created. Creates are grouped by :func:`build_apply_creates` only.
    """
    rows = [
        _s(shared_id="dry-created-0", op="create", property_name="excerpt", proposed="e", template_name="DOCUMENT"),
        _s(
            shared_id="dry-created-0",
            op="create",
            property_name=TITLE_PROPERTY,
            proposed="Created",
            template_name="DOCUMENT",
        ),
        _s(shared_id="dry-created-1", op="create", property_name="excerpt", proposed="f", template_name="DOCUMENT"),
    ]
    assert build_apply_updates(rows) == []
    assert build_apply_creates(rows) == [
        {"title": "Created", "template_name": "DOCUMENT", "metadata": {"excerpt": "e"}},
        {"title": "", "template_name": "DOCUMENT", "metadata": {"excerpt": "f"}},
    ]


def test_update_and_create_rows_group_into_disjoint_payload_sets() -> None:
    """One reviewed set carrying both ops writes each exactly once."""
    rows = [
        _s(shared_id="s1", op="update", property_name="excerpt"),
        _s(shared_id="dry-created-0", op="create", property_name="excerpt", template_name="DOCUMENT"),
        _s(
            shared_id="dry-created-0",
            op="create",
            property_name=TITLE_PROPERTY,
            proposed="Created",
            template_name="DOCUMENT",
        ),
    ]
    assert [u["shared_id"] for u in build_apply_updates(rows)] == ["s1"]
    assert build_apply_creates(rows) == [
        {"title": "Created", "template_name": "DOCUMENT", "metadata": {"excerpt": "new value"}}
    ]


def test_create_rows_merge_their_languages_into_one_payload() -> None:
    rows = [
        _s(
            shared_id="dry-created-0",
            op="create",
            language="es",
            proposed=[{"value": "hola", "language": "es"}],
            template_name="DOCUMENT",
        ),
        _s(
            shared_id="dry-created-0",
            op="create",
            language="en",
            proposed=[{"value": "hello", "language": "en"}],
            template_name="DOCUMENT",
        ),
    ]
    creates = build_apply_creates(rows, [("dry-created-0", "es"), ("dry-created-0", "en")])
    assert creates[0]["metadata"]["excerpt"] == [
        {"value": "hola", "language": "es"},
        {"value": "hello", "language": "en"},
    ]


def test_a_create_without_a_template_is_refused() -> None:
    rows = [_s(shared_id="dry-created-0", op="create", property_name="excerpt")]
    with pytest.raises(ApplyRefusedError):
        build_apply_creates(rows)


# --- the row's language reaches the written value ------------------------------


def test_a_scalar_value_is_written_with_the_row_language() -> None:
    """A bare text value must carry the language it is approved for.

    Uwazi stores text as a list of ``{"value", "language"}`` entries and
    REPLACES a property's whole list on update, so writing the bare scalar would
    land a language-agnostic value: the Spanish row would overwrite the property
    instead of joining it, and would not read back as Spanish.
    """
    updates = build_apply_updates(
        [_s(shared_id="elfd96ccepk", property_name="document_ref", proposed="A/HRC/37/NGO/92 ES", language="es")]
    )
    assert updates[0]["metadata"]["document_ref"] == [{"value": "A/HRC/37/NGO/92 ES", "language": "es"}]


def test_a_value_that_names_its_own_language_is_not_restamped() -> None:
    rows = [
        _s(
            shared_id="s1",
            language="es",
            proposed=[{"value": "hola", "language": "es"}],
        )
    ]
    assert build_apply_updates(rows)[0]["metadata"]["excerpt"] == [{"value": "hola", "language": "es"}]


def test_a_language_agnostic_row_writes_its_value_untouched() -> None:
    """A select/date/link has no language: the payload is exactly what was reviewed."""
    updates = build_apply_updates([_s(property_name="document_status", proposed="Published")])
    assert updates[0]["metadata"]["document_status"] == "Published"


def test_a_non_string_value_is_never_stamped() -> None:
    updates = build_apply_updates([_s(property_name="pages", proposed=42, language="es")])
    assert updates[0]["metadata"]["pages"] == 42


def test_a_language_agnostic_value_is_never_stamped() -> None:
    assert stamp_language(None, "es") is None
    assert stamp_language("x", None) == "x"
    assert stamp_language(["a", "b"], "es") == ["a", "b"]
    assert stamp_language({"label": "t", "url": "u"}, "es") == {"label": "t", "url": "u"}


def test_the_title_is_never_stamped() -> None:
    """The entity title is one string on the row, not a per-language entry."""
    updates = build_apply_updates([_s(property_name=TITLE_PROPERTY, proposed="New", language="es")])
    assert updates[0]["title"] == "New"
    assert "metadata" in updates[0]


def test_two_languages_of_one_property_write_one_value_list() -> None:
    """Checked ``es`` + ``en`` merge into ONE payload, tagged per language."""
    rows = [
        _s(shared_id="s1", language="es", property_name="document_ref", proposed="hola"),
        _s(shared_id="s1", language="en", property_name="document_ref", proposed="hello"),
    ]
    updates = build_apply_updates(rows, [("s1", "es"), ("s1", "en")])
    assert updates[0]["metadata"]["document_ref"] == [
        {"value": "hola", "language": "es"},
        {"value": "hello", "language": "en"},
    ]


def test_the_same_language_twice_keeps_the_last_value() -> None:
    rows = [
        _s(shared_id="s1", language="es", property_name="a", proposed="first"),
        _s(shared_id="s1", language="es", property_name="a", proposed="second"),
    ]
    updates = build_apply_updates(rows)
    assert updates[0]["metadata"]["a"] == [{"value": "second", "language": "es"}]


def test_a_created_entities_value_is_written_with_its_language() -> None:
    creates = build_apply_creates(
        [
            _s(
                shared_id="dry-created-0",
                op="create",
                property_name="document_ref",
                proposed="hola",
                language="es",
                template_name="DOCUMENT",
            )
        ]
    )
    assert creates[0]["metadata"]["document_ref"] == [{"value": "hola", "language": "es"}]


def test_an_edited_value_is_stamped_with_the_row_language() -> None:
    """The operator typed a bare string in the review table; it is written in that language."""
    rows = [_s(language="es", property_name="document_ref", status=SuggestionStatus.EDITED, edited='"typed"')]
    updates = build_apply_updates(rows)
    assert updates[0]["metadata"]["document_ref"] == [{"value": "typed", "language": "es"}]


# --- partition_for_apply -------------------------------------------------------


def test_partition_property_rows_vs_whole_entity_rows() -> None:
    rows = [
        _s(shared_id="u", status=SuggestionStatus.ACCEPTED),  # update+property -> property path
        _s(shared_id="d", op="delete", property_name=None),  # whole-entity op -> script path
        _s(shared_id="p", op="publish", property_name=None),  # whole-entity op -> script path
        _s(shared_id="x", status=SuggestionStatus.PENDING, property_name="k"),  # pending: not pushable at all
    ]
    property_rows, whole_entity_rows = partition_for_apply(rows)
    assert [r.shared_id for r in property_rows] == ["u"]
    assert [r.shared_id for r in whole_entity_rows] == ["d", "p"]


def test_partition_keeps_edited_rows_in_both_paths() -> None:
    rows = [
        _s(shared_id="u", status=SuggestionStatus.EDITED, edited='"x"'),
        _s(shared_id="d", status=SuggestionStatus.EDITED, op="delete", property_name=None),
    ]
    property_rows, whole_entity_rows = partition_for_apply(rows)
    assert [r.shared_id for r in property_rows] == ["u"]
    assert [r.shared_id for r in whole_entity_rows] == ["d"]


def test_partition_sends_every_create_to_the_script_path() -> None:
    """A create's sharedId is the dry-created-N placeholder: only the apply
    script's create mode (TARGET_SHARED_ID None) can write it."""
    rows = [
        _s(shared_id="dry-created-0", op="create", property_name="title_field"),
        _s(shared_id="dry-created-1", op="create", property_name=None),
    ]
    property_rows, whole_entity_rows = partition_for_apply(rows)
    assert property_rows == []
    assert [r.shared_id for r in whole_entity_rows] == ["dry-created-0", "dry-created-1"]


def test_partition_honors_the_subset() -> None:
    rows = [_s(shared_id="a"), _s(shared_id="b"), _s(shared_id="d", op="delete", property_name=None)]
    property_rows, whole_entity_rows = partition_for_apply(rows, [("a", None), ("d", None)])
    assert [r.shared_id for r in property_rows] == ["a"]
    assert [r.shared_id for r in whole_entity_rows] == ["d"]


def test_partition_empty_selection() -> None:
    assert partition_for_apply([]) == ([], [])
    rows = [_s(shared_id="a")]
    assert partition_for_apply(rows, []) == ([], [])


def test_partition_ignores_applied_rows() -> None:
    rows = [_s(shared_id="a", status=SuggestionStatus.APPLIED)]
    assert partition_for_apply(rows) == ([], [])
