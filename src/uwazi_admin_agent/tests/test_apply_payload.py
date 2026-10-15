"""Isolated unit tests for the apply payload grouping (per AGENTS.md).

Pure: literal :class:`Suggestion` lists in, payload dicts out. No mocks, no I/O,
no instance.
"""

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionStatus
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    build_apply_updates,
    effective_value,
    partition_for_apply,
    pushable_rows,
)


def _s(
    *,
    shared_id: str = "s1",
    op: str = "update",
    property_name: str | None = "excerpt",
    status: SuggestionStatus = SuggestionStatus.ACCEPTED,
    proposed: object = "new value",
    edited: str | None = None,
) -> Suggestion:
    return Suggestion(
        run_id="run1",
        attempt=1,
        shared_id=shared_id,
        op=op,  # type: ignore[arg-type]
        property_name=property_name,
        proposed_value=proposed,
        status=status,
        edited_value=edited,
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
    rows = [_s(shared_id="a"), _s(shared_id="b"), _s(shared_id="c")]
    assert [r.shared_id for r in pushable_rows(rows, ["a", "c"])] == ["a", "c"]
    assert pushable_rows(rows, []) == []


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
    updates = build_apply_updates(rows, ["b"])
    assert [u["shared_id"] for u in updates] == ["b"]


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
    property_rows, whole_entity_rows = partition_for_apply(rows, ["a", "d"])
    assert [r.shared_id for r in property_rows] == ["a"]
    assert [r.shared_id for r in whole_entity_rows] == ["d"]


def test_partition_empty_selection() -> None:
    assert partition_for_apply([]) == ([], [])
    rows = [_s(shared_id="a")]
    assert partition_for_apply(rows, []) == ([], [])


def test_partition_ignores_applied_rows() -> None:
    rows = [_s(shared_id="a", status=SuggestionStatus.APPLIED)]
    assert partition_for_apply(rows) == ([], [])
