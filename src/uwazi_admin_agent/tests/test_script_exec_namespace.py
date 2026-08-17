import builtins

import pytest

from uwazi_admin_agent.use_cases.script_exec_namespace import (
    SAFE_BUILTINS,
    ScopeViolationError,
    assert_ids_in_scope,
    filter_ids_to_scope,
)

# --- filter_ids_to_scope ----------------------------------------------------


def test_filter_ids_to_scope_keeps_in_scope_preserving_order() -> None:
    scope = {"A", "C"}
    assert filter_ids_to_scope(["A", "B", "C", "D", "A"], scope) == ["A", "C", "A"]


def test_filter_ids_to_scope_empty_when_none_in_scope() -> None:
    assert filter_ids_to_scope(["X", "Y"], {"A"}) == []


def test_filter_ids_to_scope_empty_input() -> None:
    assert filter_ids_to_scope([], {"A"}) == []


# --- assert_ids_in_scope ----------------------------------------------------


def test_assert_ids_in_scope_passes_when_all_in_scope() -> None:
    assert_ids_in_scope(["A", "B"], {"A", "B"}, "update_entities")  # no raise


def test_assert_ids_in_scope_passes_on_empty() -> None:
    assert_ids_in_scope([], {"A"}, "delete_entities")  # no raise


def test_assert_ids_in_scope_raises_on_out_of_scope() -> None:
    with pytest.raises(ScopeViolationError) as excinfo:
        assert_ids_in_scope(["A", "REAL_ID"], {"A"}, "delete_entities")
    assert "delete_entities" in str(excinfo.value)
    assert "REAL_ID" in str(excinfo.value)


def test_assert_ids_in_scope_lists_all_offenders() -> None:
    with pytest.raises(ScopeViolationError) as excinfo:
        assert_ids_in_scope(["X", "Y", "A"], {"A"}, "update_entities")
    msg = str(excinfo.value)
    assert "X" in msg and "Y" in msg and "A" not in msg[msg.index("refused") :]


# --- SAFE_BUILTINS: allowed names present -----------------------------------


@pytest.mark.parametrize("name", ["len", "range", "print", "dict", "list", "sorted", "str", "int", "isinstance"])
def test_safe_builtins_includes_common_names(name: str) -> None:
    assert name in SAFE_BUILTINS
    assert SAFE_BUILTINS[name] is getattr(builtins, name)


# --- SAFE_BUILTINS: escape vectors absent -----------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "open",
        "__import__",
        "eval",
        "exec",
        "compile",
        "globals",
        "locals",
        "vars",
        "dir",
        "getattr",
        "setattr",
        "delattr",
        "hasattr",
        "input",
        "breakpoint",
        "exit",
        "quit",
    ],
)
def test_safe_builtins_excludes_escape_vectors(name: str) -> None:
    assert name not in SAFE_BUILTINS


def test_safe_builtins_does_not_provide_import() -> None:
    # The script must not be able to import anything; neither __import__ nor a
    # builtins module reference is present.
    assert "__import__" not in SAFE_BUILTINS
    assert "builtins" not in SAFE_BUILTINS
    assert "__builtins__" not in SAFE_BUILTINS


# --- SAFE_BUILTINS is a real dict usable as exec __builtins__ ---------------


def test_safe_builtins_supports_basic_exec() -> None:
    ns: dict = {"__builtins__": SAFE_BUILTINS}
    exec("x = len([1, 2, 3])", ns)  # noqa: S102
    assert ns["x"] == 3


def test_safe_builtins_blocks_open_in_exec() -> None:
    ns: dict = {"__builtins__": SAFE_BUILTINS}
    with pytest.raises(NameError):
        exec("open('/etc/passwd')", ns)  # noqa: S102


def test_safe_builtins_blocks_getattr_in_exec() -> None:
    ns: dict = {"__builtins__": SAFE_BUILTINS}
    with pytest.raises(NameError):
        exec("getattr(1, 'real')", ns)  # noqa: S102


def test_safe_builtins_blocks_import_in_exec() -> None:
    ns: dict = {"__builtins__": SAFE_BUILTINS}
    # `import` resolves `__import__` from __builtins__; without it the statement
    # cannot succeed. The exact exception type is CPython-internal, so accept the
    # usual suspects.
    with pytest.raises((ImportError, NameError, KeyError)):
        exec("import os", ns)  # noqa: S102
