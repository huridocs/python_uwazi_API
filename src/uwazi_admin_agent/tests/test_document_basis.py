"""Isolated unit tests for the inference-basis value reader (pure, offline)."""

from uwazi_admin_agent.drivers.web.run_service import basis_values


def test_basis_values_reads_the_title_and_metadata_cells() -> None:
    raw = {"title": "T", "metadata": {"document_ref": [{"value": "X"}]}}
    # The unknown name must still appear in the result (the pane renders it as
    # an empty value) — that is what keeps the requested order visible.
    assert basis_values(raw, ["_title", "document_ref", "absent"]) == {
        "_title": "T",
        "document_ref": [{"value": "X"}],
        "absent": None,
    }
