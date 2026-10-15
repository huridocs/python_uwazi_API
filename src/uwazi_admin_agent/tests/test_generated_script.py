"""Unit tests for the single-script GeneratedScript model."""

from pydantic import ValidationError
import pytest

from uwazi_admin_agent.domain.generated_script import GeneratedScript


def test_generated_script_holds_one_script_body() -> None:
    s = GeneratedScript(python_code="result = 1")
    assert s.python_code == "result = 1"
    assert s.description is None


def test_generated_script_requires_python_code() -> None:
    with pytest.raises(ValidationError):
        GeneratedScript(python_code="")


def test_generated_script_description_roundtrip() -> None:
    s = GeneratedScript(python_code="x = 1", description="fills one excerpt")
    assert s.description == "fills one excerpt"
