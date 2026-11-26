"""Offline unit tests for the task CSV import seam (pure).

Per ``AGENTS.md``: literal inputs, plain assertions, no mocks, no network, no
NiceGUI rendering.
"""

from uwazi_admin_agent.domain.task_export import tasks_csv
from uwazi_admin_agent.domain.task_import import (
    ImportedTask,
    assign_names,
    parse_tasks_csv,
)


def test_export_round_trips_through_the_import_parser() -> None:
    exported = tasks_csv(
        [
            {
                "run_id": "classify",
                "status": "planned",
                "prompt": "Line one\nLine two",
                "validated_prompt": "Line one\nLine two, clarified",
            }
        ]
    )

    result = parse_tasks_csv(exported)

    assert result.ok
    assert result.skipped == ()
    assert result.tasks == (ImportedTask("classify", "Line one\nLine two", "Line one\nLine two, clarified"),)


def test_import_tolerates_a_byte_order_mark_and_strips_cells() -> None:
    text = '\ufeff"run_id","prompt"\r\n"  spaced  ","  do the thing  "\r\n'

    result = parse_tasks_csv(text)

    assert result.ok
    assert result.tasks[0].run_id == "spaced"
    assert result.tasks[0].prompt == "do the thing"


def test_a_row_without_prompt_is_skipped_and_the_rest_imported() -> None:
    result = parse_tasks_csv('"run_id","prompt"\n"good","work"\n"empty",""\n')

    assert [task.run_id for task in result.tasks] == ["good"]
    assert result.skipped and "no prompt" in result.skipped[0]


def test_duplicate_run_ids_and_unsafe_names_are_skipped() -> None:
    text = '"run_id","prompt"\n"same","a"\n"same","b"\n"../escape","c"\n'

    result = parse_tasks_csv(text)

    assert [task.run_id for task in result.tasks] == ["same"]
    assert any("duplicate" in reason for reason in result.skipped)
    assert any("cannot contain" in reason for reason in result.skipped)


def test_a_validated_prompt_equal_to_the_prompt_is_dropped() -> None:
    result = parse_tasks_csv('"run_id","prompt","validated_prompt"\n"r","same text","same text"\n')

    assert result.tasks[0].validated_prompt is None


def test_a_missing_required_column_is_a_clear_error() -> None:
    result = parse_tasks_csv('"name","prompt"\n"x","y"\n')

    assert not result.ok
    assert "'run_id'" in result.error


def test_empty_file_is_an_error() -> None:
    assert parse_tasks_csv("   ").error == "The file is empty"


def test_assign_names_keeps_free_names_and_renames_collisions() -> None:
    tasks = (ImportedTask("fresh", "p"), ImportedTask("taken", "p"), ImportedTask("taken copy", "p"))

    planned = assign_names(tasks, ["taken"])

    assert [name for name, _task in planned] == ["fresh", "taken copy", "taken copy copy"]
    assert [task.run_id for _name, task in planned] == ["fresh", "taken", "taken copy"]
