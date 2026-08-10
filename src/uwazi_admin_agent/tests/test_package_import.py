import importlib

from uwazi_admin_agent.configuration import (
    DEFAULT_STORE_DIR,
    EXECUTE_BATCH_SIZE,
    MAX_ENTITIES_PER_RUN,
)


def test_import_package_succeeds() -> None:
    pkg = importlib.import_module("uwazi_admin_agent")
    assert pkg.__doc__ is not None
    assert "Operating model" in pkg.__doc__


def test_configuration_constants_present() -> None:
    assert isinstance(DEFAULT_STORE_DIR, __import__("pathlib").Path)
    # Resolved to absolute, so no leading "." leaks to consumers.
    assert DEFAULT_STORE_DIR.is_absolute()
    assert DEFAULT_STORE_DIR.name == "runs"
    assert DEFAULT_STORE_DIR.parent.name == ".uwazi_admin_agent"
    assert isinstance(MAX_ENTITIES_PER_RUN, int) and MAX_ENTITIES_PER_RUN > 0
    assert isinstance(EXECUTE_BATCH_SIZE, int) and EXECUTE_BATCH_SIZE > 0
