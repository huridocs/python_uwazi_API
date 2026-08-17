import pytest
from pydantic import ValidationError

from uwazi_admin_agent.domain.generated_script import GeneratedScript


def test_generated_script_accepts_code() -> None:
    s = GeneratedScript(python_code="result = [e for e in entities]")
    assert s.python_code == "result = [e for e in entities]"
    assert s.description is None


def test_generated_script_rejects_empty_code() -> None:
    with pytest.raises(ValidationError):
        GeneratedScript(python_code="")


def test_generated_script_carries_optional_description() -> None:
    s = GeneratedScript(python_code="x = 1", description="uppercases titles")
    assert s.description == "uppercases titles"
