"""Isolated unit tests for the apply payload grouping (per AGENTS.md).

Pure: literal :class:`Suggestion` lists in, payload dicts out. No mocks, no I/O,
no instance.

The grouping is per (entity, LANGUAGE) because that is what Uwazi stores: a
translation is a separate entity-language row, selected by the write's ``locale``
cookie, and a text property holds ONE value entry on that row. The tests below
therefore state the two rules that a per-language Run depends on — one payload
per row, and one value entry per payload — alongside the per-entity rules.
"""

import pytest

from uwazi_admin_agent.domain.suggestion import TITLE_PROPERTY, Suggestion, SuggestionStatus
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import (
    ApplyRefusedError,
    ApplyWriteError,
    build_apply_creates,
    build_apply_updates,
    effective_value,
    format_write_failures,
    language_groups,
    language_scoped_value,
    partition_for_apply,
    pushable_rows,
    stamp_language,
    write_failures,
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
        {"shared_id": "s1", "language": None, "metadata": {"excerpt": "e", "topic": ["t"]}},
        {"shared_id": "s2", "language": None, "metadata": {"excerpt": "other"}},
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
    assert updates == [{"shared_id": "s1", "language": None, "metadata": {"excerpt": "e"}, "title": "New Title"}]


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


# --- one payload per entity-language ROW --------------------------------------


def test_two_checked_languages_of_one_property_are_two_payloads() -> None:
    """One payload per ROW: Uwazi stores a translation as a separate entity row.

    Merging the two halves into ONE payload carrying a two-entry value list is
    what Uwazi refuses with *"Text Property only accepts a single value."* — the
    review then reported a completed Run over an entity it never changed.
    """
    rows = [
        _s(language="es", proposed=[{"value": "hola", "language": "es"}]),
        _s(language="en", proposed=[{"value": "hello", "language": "en"}]),
    ]

    updates = build_apply_updates(rows, [("s1", "es"), ("s1", "en")])

    assert [(u["shared_id"], u["language"]) for u in updates] == [("s1", "es"), ("s1", "en")]
    assert updates[0]["metadata"]["excerpt"] == [{"value": "hola", "language": "es"}]
    assert updates[1]["metadata"]["excerpt"] == [{"value": "hello", "language": "en"}]


def test_one_checked_language_writes_only_that_language() -> None:
    rows = [
        _s(language="es", proposed=[{"value": "hola", "language": "es"}]),
        _s(language="en", proposed=[{"value": "hello", "language": "en"}]),
    ]

    updates = build_apply_updates(rows, [("s1", "es")])

    assert len(updates) == 1
    assert updates[0]["language"] == "es"
    assert updates[0]["metadata"]["excerpt"] == [{"value": "hola", "language": "es"}]


def test_a_multi_language_proposal_collapses_to_the_rows_own_entry() -> None:
    """A single row carrying both languages still writes ONE entry — its own."""
    rows = [
        _s(
            language="es",
            proposed=[
                {"value": "hola", "language": "es"},
                {"value": "hello", "language": "en"},
            ],
        )
    ]

    updates = build_apply_updates(rows)

    assert updates[0]["metadata"]["excerpt"] == [{"value": "hola", "language": "es"}]


# --- language_scoped_value ----------------------------------------------------


def test_language_scoped_value_keeps_the_languages_own_entry() -> None:
    """The point: writing es must send ONE entry — the entity's en row is another row."""
    value = [{"value": "hello", "language": "en"}, {"value": "hola", "language": "es"}]

    assert language_scoped_value(value, "es") == [{"value": "hola", "language": "es"}]


def test_language_scoped_value_never_widens_a_value_list() -> None:
    assert len(language_scoped_value([{"value": "a", "language": "en"}, {"value": "b", "language": "es"}], "es")) == 1


def test_language_scoped_value_falls_back_to_the_first_entry() -> None:
    """A row naming a language the value does not carry still stores one entry."""
    assert language_scoped_value([{"value": "hello", "language": "en"}], "de") == [{"value": "hello", "language": "en"}]
    assert language_scoped_value([{"value": "hola", "language": "es"}], None) == [{"value": "hola", "language": "es"}]


def test_language_scoped_value_leaves_a_language_independent_value_alone() -> None:
    """A select is not a language list: Uwazi replaces it whole, which is the proposal's meaning."""
    assert language_scoped_value("Open", "es") == [{"value": "Open", "language": "es"}]
    assert language_scoped_value([{"label": "T1", "value": "t1"}], "es") == [{"label": "T1", "value": "t1"}]
    assert language_scoped_value({"label": "S", "url": "u"}, None) == {"label": "S", "url": "u"}


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

    Enriching an update payload reads that entity's raw to resolve its template,
    so a create reaching this grouping aborted the whole Run with
    ``get_raw_by_shared_id(dry-created-0) failed (404)`` before a single entity was
    created. Creates are grouped by :func:`build_apply_creates` only.
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
        {
            "title": "Created",
            "template_name": "DOCUMENT",
            "metadata": {"excerpt": "e"},
            "language": None,
            "source_shared_id": "dry-created-0",
        },
        {
            "title": "",
            "template_name": "DOCUMENT",
            "metadata": {"excerpt": "f"},
            "language": None,
            "source_shared_id": "dry-created-1",
        },
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
        {
            "title": "Created",
            "template_name": "DOCUMENT",
            "metadata": {"excerpt": "new value"},
            "language": None,
            "source_shared_id": "dry-created-0",
        }
    ]


def test_create_rows_become_one_payload_per_language() -> None:
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
    assert [(c["language"], c["metadata"]["excerpt"]) for c in creates] == [
        ("es", [{"value": "hola", "language": "es"}]),
        ("en", [{"value": "hello", "language": "en"}]),
    ]


def test_a_create_without_a_template_is_refused() -> None:
    rows = [_s(shared_id="dry-created-0", op="create", property_name="excerpt")]
    with pytest.raises(ApplyRefusedError):
        build_apply_creates(rows)


# --- the row's language reaches the written value ------------------------------


def test_a_scalar_value_is_written_with_the_row_language() -> None:
    """A bare text value must carry the language it is approved for.

    Uwazi keeps text as a single ``{"value": ...}`` entry on the entity-language
    ROW the write targets, so writing the bare scalar — or a value list tagged
    with another language — would land the wrong text on the row.
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


def test_two_languages_of_one_property_write_two_payloads() -> None:
    """Checked ``es`` + ``en``: one payload each, targeting one entity row each."""
    rows = [
        _s(shared_id="s1", language="es", property_name="document_ref", proposed="hola"),
        _s(shared_id="s1", language="en", property_name="document_ref", proposed="hello"),
    ]
    updates = build_apply_updates(rows, [("s1", "es"), ("s1", "en")])
    assert [(u["language"], u["metadata"]["document_ref"]) for u in updates] == [
        ("es", [{"value": "hola", "language": "es"}]),
        ("en", [{"value": "hello", "language": "en"}]),
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


# --- write_failures: the read that makes a refusal visible --------------------


UPLOAD_422 = "Error uploading entity 422 {&#34;prettyMessage&#34;: &#34;/: Text Property only accepts a single value.&#34;}"


def test_write_failures_reports_a_refused_update() -> None:
    """A ``success=False`` result is the only trace of a write Uwazi rejected.

    Ignoring it is what let a 422 be reported to the operator as a completed Run
    over an entity that was never changed.
    """
    results = [{"shared_id": "ok", "success": True}, {"shared_id": "hq6qzufxwud", "success": False, "error": UPLOAD_422}]

    assert write_failures(results) == [{"shared_id": "hq6qzufxwud", "error": UPLOAD_422}]


def test_write_failures_reads_the_publish_summary_shape() -> None:
    summary = {
        "success_count": 1,
        "failure_count": 1,
        "rate_limited": [],
        "permission_denied": [],
        "not_found": [],
        "errors": [{"shared_id": "a", "error": "not found"}],
    }
    assert write_failures(summary) == [{"shared_id": "a", "error": "not found"}]


def test_write_failures_counts_a_summary_without_error_rows() -> None:
    assert write_failures({"success_count": 1, "failure_count": 2, "errors": []}) == [
        {"shared_id": "", "error": "2 entity/entities could not be updated"}
    ]


def test_write_failures_attributes_a_refused_create_to_its_placeholder() -> None:
    """Uwazi mints no sharedId on failure, so the placeholder is the only identity."""
    results = [{"shared_id": "", "success": False, "error": "boom"}]

    assert write_failures(results, ["dry-created-3"]) == [{"shared_id": "dry-created-3", "error": "boom"}]


def test_write_failures_is_empty_for_a_clean_result() -> None:
    assert write_failures([{"shared_id": "a", "success": True}]) == []
    assert write_failures({"success_count": 3, "failure_count": 0, "errors": []}) == []
    assert write_failures([]) == []
    assert write_failures(None) == []


def test_write_failures_trims_uwazis_response_body() -> None:
    """Uwazi returns the whole JSON validation dump; a toast cannot show it."""
    failure = write_failures([{"shared_id": "a", "success": False, "error": "x" * 1000}])[0]

    assert failure["error"].endswith("…")
    assert len(failure["error"]) < 500


def test_write_failures_falls_back_to_the_error_code() -> None:
    assert write_failures([{"shared_id": "a", "success": False, "error_code": "RATE_LIMITED"}])[0]["error"] == (
        "RATE_LIMITED"
    )


# --- format_write_failures / ApplyWriteError ----------------------------------


def test_format_write_failures_names_each_refused_write() -> None:
    message = format_write_failures([{"shared_id": "a", "error": "boom"}])

    assert "1 write(s) refused by Uwazi" in message
    assert "a: boom" in message


def test_format_write_failures_caps_the_listing() -> None:
    failures = [{"shared_id": f"s{i}", "error": "boom"} for i in range(5)]

    message = format_write_failures(failures)

    assert "(+2 more)" in message
    assert "s4" not in message


def test_apply_write_error_carries_the_failures_and_reads_as_one_line() -> None:
    error = ApplyWriteError([{"shared_id": "a", "error": "boom"}])

    assert error.failures == [{"shared_id": "a", "error": "boom"}]
    assert "a: boom" in str(error)


# --- language_groups: one write call per entity-language row -------------------


def test_language_groups_splits_the_call_per_language() -> None:
    """``locale`` picks the ROW, so both halves of a translation need two calls."""
    payloads = [
        {"shared_id": "s1", "language": "es"},
        {"shared_id": "s1", "language": "en"},
        {"shared_id": "s2", "language": "es"},
    ]

    groups = language_groups(payloads, "en")

    assert [language for language, _batch in groups] == ["es", "en"]
    assert [p["shared_id"] for p in groups[0][1]] == ["s1", "s2"]
    assert [p["shared_id"] for p in groups[1][1]] == ["s1"]


def test_language_groups_falls_back_to_the_run_default_and_stamps_it() -> None:
    """The entity models declare ``language`` as a plain string, so it must be set."""
    groups = language_groups([{"shared_id": "s1"}, {"shared_id": "s2", "language": None}], "en")

    assert [language for language, _batch in groups] == ["en"]
    assert groups[0][1][0]["language"] == "en"


def test_language_groups_on_empty_input() -> None:
    assert language_groups([], "en") == []
