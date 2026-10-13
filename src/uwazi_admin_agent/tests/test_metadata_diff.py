"""Pure tests for the applied-write property diff (results window).

No I/O, no mocks: :func:`build_property_changes` and
:func:`format_metadata_value` are deterministic transforms of a literal
:class:`AppliedWrite`, and the manifest cases exercise pydantic defaults only.
"""

from uwazi_admin_agent.domain.manifest import AppliedWrite, MigrationManifest
from uwazi_admin_agent.domain.metadata_diff import build_property_changes, format_metadata_value


def _write(**kwargs: object) -> AppliedWrite:
    base: dict[str, object] = {"op": "update", "shared_id": "S1"}
    base.update(kwargs)
    return AppliedWrite(**base)  # type: ignore[arg-type]


def test_build_property_changes_update() -> None:
    write = _write(
        title="New Title",
        title_before="Old Title",
        metadata={"excerpt": [{"value": "after"}], "topic": ["New"]},
        metadata_before={"excerpt": [{"value": "before"}], "topic": ["New"]},
    )

    changes = build_property_changes(write)

    assert [(c.property, c.before, c.after, c.changed) for c in changes] == [
        ("title", "Old Title", "New Title", True),
        ("excerpt", "before", "after", True),
        ("topic", "New", "New", False),
    ]


def test_build_property_changes_metadata_only_write_has_no_title_entry() -> None:
    write = _write(metadata={"excerpt": [{"value": "x"}]}, metadata_before={"excerpt": [{"value": "y"}]})

    changes = build_property_changes(write)

    assert [c.property for c in changes] == ["excerpt"]
    assert changes[0].changed is True


def test_build_property_changes_publish_unpublish() -> None:
    unpublish = _write(op="unpublish", metadata_before={"published": True})
    publish = _write(op="publish", metadata_before={"published": False})
    already = _write(op="publish", metadata_before={"published": True})

    assert [(c.property, c.before, c.after, c.changed) for c in build_property_changes(unpublish)] == [
        ("published", "true", "false", True)
    ]
    assert [(c.before, c.after, c.changed) for c in build_property_changes(publish)] == [("false", "true", True)]
    assert [c.changed for c in build_property_changes(already)] == [False]


def test_build_property_changes_create_has_wildcard_before() -> None:
    write = _write(op="create", title="Created", metadata={"excerpt": [{"value": "v"}]})

    changes = build_property_changes(write)

    assert [(c.property, c.before, c.after, c.changed) for c in changes] == [
        ("title", "—", "Created", True),
        ("excerpt", "—", "v", True),
    ]


def test_format_metadata_value_shapes() -> None:
    assert format_metadata_value(None) == "—"
    assert format_metadata_value(True) == "true"
    assert format_metadata_value(False) == "false"
    assert format_metadata_value("plain") == "plain"
    assert format_metadata_value(42) == "42"
    assert format_metadata_value([{"value": "uuid", "label": "The Label"}]) == "The Label"
    assert format_metadata_value([{"value": "uuid"}]) == "uuid"
    assert format_metadata_value(["a", "b"]) == "a, b"
    assert format_metadata_value([{"value": "a"}, {"value": "b"}]) == "a, b"
    assert format_metadata_value([]) == ""
    assert format_metadata_value("x" * 200) == "x" * 200

    truncated = format_metadata_value("x" * 201)
    assert truncated == "x" * 200 + "…"


def test_manifest_legacy_load_defaults_applied_writes() -> None:
    manifest = MigrationManifest.model_validate(
        {"run_id": "r", "created_at": "2026-01-01T00:00:00Z", "prompt": "p", "script": "s"}
    )

    assert manifest.applied_writes == []


def test_reset_touch_set_clears_applied_writes() -> None:
    manifest = MigrationManifest.model_validate(
        {"run_id": "r", "created_at": "2026-01-01T00:00:00Z", "prompt": "p", "script": "s"}
    )
    manifest.applied_writes.append(_write(title="T", metadata={"excerpt": [{"value": "v"}]}))
    manifest.result = "summary"

    manifest.reset_touch_set()

    assert manifest.applied_writes == []
    assert manifest.result is None
