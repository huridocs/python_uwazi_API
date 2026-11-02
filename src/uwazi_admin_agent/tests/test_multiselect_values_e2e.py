"""Live E2E: multiselect value-level accept/reject through the service layer.

Proves the whole loop against a real Uwazi on localhost: recording a
multiselect change ONE row, rejecting values through ``set_rejected_values``,
and the Run writing exactly the accepted labels as thesaurus UUIDs — Beta
rejected means Beta absent from the live metadata, Gamma absent because the
write is a whole-list replace.
"""

from __future__ import annotations

import pytest

from uwazi_admin_agent.domain.select_values import ALL_VALUES_REJECTED_REASON
from uwazi_admin_agent.domain.suggestion import SuggestionStatus
from uwazi_admin_agent.drivers.web.run_service import (
    discard_ids,
    restore_ids,
    set_rejected_values,
)
from uwazi_admin_agent.tests.e2e_support import (
    UWAZI_URL,
    AdminE2EWorkspace,
)
from uwazi_admin_agent.use_cases.apply_suggestions_use_case import ApplyRefusedError
from uwazi_api.testing.e2e_guard import require_local_e2e_target

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def _live_guard() -> None:
    require_local_e2e_target(UWAZI_URL)


def _labels_script(ws: AdminE2EWorkspace, shared_ids: list[str], labels: list[str]) -> str:
    """A deterministic planning script writing plain thesaurus LABELS (the script contract)."""
    language = ws.languages.default
    return (
        f"targets = {shared_ids!r}\n"
        f"update_entities(\n"
        f"    [\n"
        f"        {{'shared_id': shared_id, 'template_name': {ws.template_name!r},\n"
        f"          'language': {language!r},\n"
        f"          'metadata': { ({ws.multiselect_property: labels})!r}}}\n"
        f"        for shared_id in targets\n"
        f"    ],\n"
        f"    language={language!r},\n"
        f")\n"
        f"result = f'updated {{len(targets)}} row(s)'\n"
    )


def _multiselect_rows(ws: AdminE2EWorkspace, run_id: str) -> list:
    rows = [row for row in ws.suggestions(run_id) if row.property_name == ws.multiselect_property]
    assert rows, f"no multiselect suggestion row recorded for run {run_id}"
    return rows


def _metadata_uuids(raw: dict | None, prop: str) -> list[str]:
    cell = (raw or {}).get("metadata", {}).get(prop) or []
    return [entry["value"] if isinstance(entry, dict) else entry for entry in cell]


def _labels_via_mapping(raw: dict | None, prop: str, ws: AdminE2EWorkspace) -> list[str]:
    """The live metadata's labels, resolved through the thesaurus value ids."""
    uuid_by_label = {}
    assert ws.client is not None
    for thesaurus in ws.client.thesauris.get(ws.languages.default):
        if thesaurus.id == ws.thesaurus_id:
            uuid_by_label = {v.id: v.label for v in thesaurus.values}
    return [uuid_by_label.get(value, value) for value in _metadata_uuids(raw, prop)]


def test_multiselect_value_level_accept_reject(workspace: AdminE2EWorkspace) -> None:
    """Reject one value → Run writes only the accepted uuids; reject all → nothing."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    prop = ws.multiselect_property

    # Record ONE language-independent row proposing Alpha + Beta.
    run_id = ws.new_run(_labels_script(ws, [shared_id], ["Alpha", "Beta"]))
    rows = _multiselect_rows(ws, run_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.language is None
    assert sorted(row.proposed_value) == ["Alpha", "Beta"]
    assert row.status is SuggestionStatus.PENDING

    # Reject Beta on the recorded row.
    updated = set_rejected_values(UWAZI_URL, run_id, row.id, ["Beta"])
    assert updated.status is SuggestionStatus.PENDING
    assert updated.rejected_values == ["Beta"]

    # The Run writes exactly the accepted labels as thesaurus UUIDs.
    ws.apply(run_id, rows=[updated])
    raw = ws.raw(shared_id, language)
    labels = _labels_via_mapping(raw, prop, ws)
    assert sorted(labels) == ["Alpha"], f"expected only Alpha, got {labels}"

    # The write landed: the row reads APPLIED.
    row_after = _multiselect_rows(ws, run_id)[0]
    assert row_after.status is SuggestionStatus.APPLIED


def test_multiselect_reject_all_then_accept_all(workspace: AdminE2EWorkspace) -> None:
    """All values rejected → the row is discarded and the Run refuses; accept all → it runs."""
    ws = workspace
    shared_id = ws.entity_ids[1]
    language = ws.languages.default
    prop = ws.multiselect_property

    run_id = ws.new_run(_labels_script(ws, [shared_id], ["Alpha", "Gamma"]))
    row = _multiselect_rows(ws, run_id)[0]

    # Reject everything.
    discarded = set_rejected_values(UWAZI_URL, run_id, row.id, ["Alpha", "Gamma"])
    assert discarded.status is SuggestionStatus.DISCARDED
    assert discarded.discard_reason == ALL_VALUES_REJECTED_REASON

    # A Run over that selection refuses BEFORE any write: Uwazi is untouched.
    before = ws.raw(shared_id, language)
    with pytest.raises(ApplyRefusedError):
        ws.apply(run_id, rows=[discarded])
    after = ws.raw(shared_id, language)
    assert _metadata_uuids(before, prop) == _metadata_uuids(after, prop)

    # Accept every value again: the sentinel discard round-trips to PENDING.
    restored = set_rejected_values(UWAZI_URL, run_id, row.id, [])
    assert restored.status is SuggestionStatus.PENDING
    assert restored.discard_reason is None
    assert restored.rejected_values == []

    # The Run writes both values now.
    ws.apply(run_id, rows=[restored])
    labels = _labels_via_mapping(ws.raw(shared_id, language), prop, ws)
    assert sorted(labels) == ["Alpha", "Gamma"]


def test_set_rejected_values_refuses_applied_and_other_discards(workspace: AdminE2EWorkspace) -> None:
    """The guard rails: APPLIED rows and a real discard are not edited."""
    ws = workspace
    shared_id = ws.entity_ids[0]
    language = ws.languages.default
    prop = ws.multiselect_property

    run_id = ws.new_run(_labels_script(ws, [shared_id], ["Alpha"]))
    row = _multiselect_rows(ws, run_id)[0]

    # A real discard (another reason) must not be edited.
    discard_ids(UWAZI_URL, run_id, [row.id], "no longer wanted")
    with pytest.raises(ValueError) as err:
        set_rejected_values(UWAZI_URL, run_id, row.id, ["Alpha"])
    assert "restore it first" in str(err.value)

    restore_ids(UWAZI_URL, run_id, [row.id])
    ws.apply(run_id, rows=[row])
    with pytest.raises(ValueError) as err:
        set_rejected_values(UWAZI_URL, run_id, row.id, ["Alpha"])
    assert "already applied" in str(err.value)
