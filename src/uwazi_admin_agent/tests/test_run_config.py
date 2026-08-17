import pytest
from pydantic import ValidationError

from uwazi_admin_agent.domain.run_config import RunConfig


def test_run_config_accepts_name_and_prompt() -> None:
    cfg = RunConfig(name="repair-titles", prompt="Set every Court title to uppercase")
    assert cfg.name == "repair-titles"
    assert cfg.prompt == "Set every Court title to uppercase"


def test_run_config_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        RunConfig(name="", prompt="p")


def test_run_config_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        RunConfig(name="n", prompt="")
