from uwazi_admin_agent.adapters.script_emitter import SCRIPT_FILENAME, emit_generated_script
from uwazi_admin_agent.domain.generated_script import GeneratedScript


def test_emit_writes_script_to_stable_name(tmp_path) -> None:
    run_path = tmp_path / "r1"
    script = GeneratedScript(python_code="x = 1\nprint(x)\n", description="uppercase titles")

    path = emit_generated_script(script, run_path)

    assert path == run_path / SCRIPT_FILENAME
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == "x = 1\nprint(x)\n"


def test_emit_creates_run_folder_if_missing(tmp_path) -> None:
    run_path = tmp_path / "deep" / "r1"
    assert not run_path.exists()

    path = emit_generated_script(GeneratedScript(python_code="pass\n"), run_path)

    assert path.is_file()
    assert run_path.is_dir()


def test_emit_overwrites_existing_script(tmp_path) -> None:
    run_path = tmp_path / "r1"
    emit_generated_script(GeneratedScript(python_code="old = 1\n"), run_path)

    path = emit_generated_script(GeneratedScript(python_code="new = 2\n"), run_path)

    assert path.read_text(encoding="utf-8") == "new = 2\n"


def test_emit_stable_name_is_script_py() -> None:
    assert SCRIPT_FILENAME == "script.py"
