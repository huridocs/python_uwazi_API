"""Pure tests for :func:`filter_in_scope_by_template` (Part C — dummy fidelity).

The dummy-mode ``by_template`` must filter in-scope dummies by ``template_name``
so a script looping ``by_template`` over several discovered templates processes
each template's dummies once (mirroring real mode). Otherwise every
``by_template`` call returns ALL in-scope dummies and the gate false-fails a
correct multi-template script (deleted source dummies reappear via the static
``dummy_entities`` list on the next iteration).

Isolated: no mocks, no network, literal ``AgentEntity`` inputs only.
"""

from __future__ import annotations

from uwazi_admin_agent.use_cases.script_exec_namespace import filter_in_scope_by_template
from uwazi_agent.domain.agent_entity import AgentEntity


def _e(sid: str, template_name: str, title: str = "T") -> AgentEntity:
    return AgentEntity(shared_id=sid, title=title, template_name=template_name)


def test_returns_only_dummies_of_the_named_template() -> None:
    in_scope = [
        _e("J1", "Judgment", "Voto"),
        _e("J2", "Judgment", "Voto"),
        _e("R1", "Report", "Voto"),
        _e("R2", "Report", "Informe"),
    ]

    out = filter_in_scope_by_template(in_scope, "Judgment")

    assert [e.shared_id for e in out] == ["J1", "J2"]


def test_empty_when_no_dummy_matches_the_template() -> None:
    in_scope = [_e("R1", "Report"), _e("R2", "Report")]

    assert filter_in_scope_by_template(in_scope, "Judgment") == []


def test_other_templates_excluded() -> None:
    in_scope = [_e("J1", "Judgment"), _e("P1", "Person"), _e("R1", "Report")]

    out = filter_in_scope_by_template(in_scope, "Person")

    assert [e.shared_id for e in out] == ["P1"]


def test_all_returned_when_template_name_is_none() -> None:
    # Defensive: by_template requires a template_name, but the helper stays total.
    in_scope = [_e("J1", "Judgment"), _e("R1", "Report")]

    out = filter_in_scope_by_template(in_scope, None)

    assert out == in_scope


def test_preserves_order_of_in_scope() -> None:
    in_scope = [
        _e("J3", "Judgment"),
        _e("R1", "Report"),
        _e("J1", "Judgment"),
        _e("J2", "Judgment"),
    ]

    out = filter_in_scope_by_template(in_scope, "Judgment")

    assert [e.shared_id for e in out] == ["J3", "J1", "J2"]


def test_does_not_mutate_input() -> None:
    in_scope = [_e("J1", "Judgment"), _e("R1", "Report")]

    _ = filter_in_scope_by_template(in_scope, "Judgment")

    assert [e.shared_id for e in in_scope] == ["J1", "R1"]


def test_empty_in_scope_yields_empty() -> None:
    assert filter_in_scope_by_template([], "Judgment") == []
