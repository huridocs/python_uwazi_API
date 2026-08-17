import importlib

from uwazi_admin_agent.configuration import (
    EXECUTE_BATCH_SIZE,
    MAX_ENTITIES_PER_RUN,
)


def test_import_package_succeeds() -> None:
    pkg = importlib.import_module("uwazi_admin_agent")
    assert pkg.__doc__ is not None
    assert "Operating model" in pkg.__doc__


def test_configuration_constants_present() -> None:
    assert isinstance(MAX_ENTITIES_PER_RUN, int) and MAX_ENTITIES_PER_RUN > 0
    assert isinstance(EXECUTE_BATCH_SIZE, int) and EXECUTE_BATCH_SIZE > 0
