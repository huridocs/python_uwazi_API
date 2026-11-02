"""Isolated tests for the results-window row helpers (pure, offline).

``_merge_changes`` / ``_change_summary`` / ``_entity_url`` / ``_row_language``
are deterministic transforms over literal inputs, so they need no Uwazi
instance and no mocks. Importing the web driver module is side-effect free (it
only defines the NiceGUI page functions).
"""

from uwazi_admin_agent.domain.manifest import AppliedWrite
from uwazi_admin_agent.drivers.web.app import _change_summary, _entity_url, _merge_changes, _row_language


def _write(**kwargs: object) -> AppliedWrite:
    base: dict[str, object] = {"op": "update", "shared_id": "S1", "language": "en"}
    base.update(kwargs)
    return AppliedWrite(**base)  # type: ignore[arg-type]


def test_merge_changes_keeps_first_before_and_last_after() -> None:
    writes = [
        _write(metadata={"excerpt": [{"value": "b"}]}, metadata_before={"excerpt": [{"value": "a"}]}),
        _write(metadata={"excerpt": [{"value": "c"}], "topic": ["T"]}, metadata_before={"excerpt": [{"value": "b"}]}),
    ]

    merged = _merge_changes(writes)

    assert [(c.property, c.before, c.after, c.changed) for c in merged] == [
        ("excerpt", "a", "c", True),
        ("topic", "—", "T", True),
    ]


def test_merge_changes_net_no_change_reads_unchanged() -> None:
    writes = [
        _write(metadata={"excerpt": [{"value": "x"}]}, metadata_before={"excerpt": [{"value": "a"}]}),
        _write(metadata={"excerpt": [{"value": "a"}]}, metadata_before={"excerpt": [{"value": "x"}]}),
    ]

    merged = _merge_changes(writes)

    assert [(c.before, c.after, c.changed) for c in merged] == [("a", "a", False)]


def test_change_summary_lists_all_when_short() -> None:
    changes = _merge_changes(
        [
            _write(
                metadata={"a": "1", "bb": "2", "same": "z"},
                metadata_before={"a": "0", "bb": "0", "same": "z"},
            )
        ]
    )

    assert _change_summary(changes) == "a, bb"
    assert _change_summary([]) == "—"


def test_change_summary_truncates_with_remainder_count() -> None:
    properties = {f"property_{i:02d}": "new" for i in range(10)}
    changes = _merge_changes([_write(metadata=properties)])

    summary = _change_summary(changes)

    assert summary.endswith("more)")
    assert len(summary.split(" (")[0]) <= 60


def test_entity_url_omits_language_default_and_unknown_instance() -> None:
    # The base url is the SESSION's instance: no instance means no link.
    assert _entity_url("S1", None, None) is None
    assert _entity_url("S1", None, "") is None
    assert _entity_url("S1", None, "http://x.io") == "http://x.io/en/entity/S1"
    assert _entity_url("S1", "es", "http://x.io/") == "http://x.io/es/entity/S1"


def test_row_language_prefers_identity_then_write() -> None:
    assert _row_language("fr", [_write(language="en")]) == "fr"
    assert _row_language(None, [_write(language="es")]) == "es"
    assert _row_language(None, [_write(language=None)]) is None
    assert _row_language(None, []) is None
