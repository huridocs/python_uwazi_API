import builtins
import datetime as _datetime_module
import random as _random_module

import pytest

from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _STDLIB,
    SAFE_BUILTINS,
    ScopeViolationError,
    assert_ids_in_scope,
    build_exec_namespace,
    build_real_exec_namespace,
    filter_ids_to_scope,
    run_script_sync,
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


# --- _STDLIB: random + datetime-as-module (create-task fix) -----------------


def test_stdlib_binds_random_module() -> None:
    """`random` is bound (pure-compute, no I/O) so 'fill with random information'
    create scripts use `random.randint/choice` instead of `__import__` or a
    hand-rolled pseudo-random."""
    assert _STDLIB["random"] is _random_module


def test_stdlib_binds_datetime_as_module_not_class() -> None:
    """`datetime` is bound as the MODULE so the script uses the standard
    `datetime.datetime.now()` / `datetime.timedelta()` idiom (the LLM's natural
    instinct) and gets `timedelta`/`date`/`time` for free. Pinning this guards the
    live bug where the LLM wrote `datetime.datetime.datetime...` (treating the
    bound name as a class while it was the module, or vice-versa)."""
    assert _STDLIB["datetime"] is _datetime_module


def test_run_script_sync_random_and_datetime_module_idiom_run_clean() -> None:
    """A script using `random.choice` and the `datetime.datetime.now()` /
    `datetime.timedelta()` module idiom must run clean under the scoped namespace.
    No network - `run_script_sync` is pure w.r.t. the namespace contents."""
    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    code = (
        "choices = random.choice(['a', 'b', 'c'])\n"
        "now = datetime.datetime.now()\n"
        "delta = datetime.timedelta(days=3)\n"
        "result = 'choice=%s; now_ok=%s; delta=%s' % (choices, isinstance(now, datetime.datetime), delta)\n"
    )
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result is not None
    assert "now_ok=True" in result
    assert "delta=3 days" in result


def test_run_script_sync_rejects_datetime_class_idiom() -> None:
    """`datetime.now()` (treating the bound name as the class) must FAIL because
    `datetime` is the module - the module has no `now`. Pins the contract so the
    LLM cannot silently use the old class-form idiom."""
    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    code = "now = datetime.now()\n"
    result, error = run_script_sync(code, ns)
    assert error is not None
    assert "AttributeError" in error


# --- htmlextract binding (extraction phase) -----------------------------------


def test_stdlib_binds_htmlextract_members() -> None:
    """`htmlextract` is bound as the fifth stdlib entry with its five members:
    text/title/tables/meta (pure parsers) + is_html (file-ref classifier)."""
    hx = _STDLIB["htmlextract"]
    for member in ("text", "title", "tables", "meta", "is_html"):
        assert callable(getattr(hx, member)), member


def test_run_script_sync_extract_idiom_runs_clean() -> None:
    """The embed idiom: a script defines `def extract(html)` using `re` +
    `htmlextract.tables`, runs it over two literal HTML strings (one matching,
    one not), and sets result. Proves the extraction contract end-to-end."""
    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    code = """
def extract(html):
    try:
        tables = htmlextract.tables(html)
        for table in tables:
            for row in table:
                for i, cell in enumerate(row):
                    if cell.strip().lower() == "case number":
                        return {"case_number": row[i + 1]}
        return None
    except Exception:
        return None

matched = extract("<table><tr><td>Case Number</td><td>42</td></tr></table>")
unmatched = extract("<p>nothing here</p>")
result = f"{1 if matched else 0}/{2 if unmatched is None else 99}"
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "1/2"


def test_run_script_sync_import_html_fails() -> None:
    """`import html` must fail (zero import lines; `__import__` is not bound)."""

    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    result, error = run_script_sync("import html\nresult = 'x'\n", ns)
    assert result is None
    assert error is not None and "ImportError" in error


# --- dummy-scoped file-fetch stubs ----------------------------------------------


def test_dummy_namespace_get_entity_files_scoped() -> None:
    """`get_entity_files` returns [] in-scope and raises ScopeViolationError
    out-of-scope; `get_file_bytes` returns None (dummies carry no files)."""
    import asyncio as _asyncio

    namespace = build_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=_asyncio.new_event_loop(),
        scope={"DUMMY1"},
        dummy_entities=[],
        tool_cache=None,
        default_language="en",
    )
    code = """
in_scope = get_entity_files("DUMMY1")
refused = False
try:
    get_entity_files("REAL_ID")
except RuntimeError:
    refused = True
missing = get_file_bytes("x.html")
result = f"{len(in_scope)}|{refused}|{missing}"
"""
    result, error = run_script_sync(code, namespace)
    assert error is None
    assert result == "0|True|None"


def test_real_namespace_unwired_file_helpers_raise_runtime_error() -> None:
    """With no repositories wired, the real `get_entity_files`/`get_file_bytes`
    fail LOUDLY (RuntimeError naming the helper) instead of silently no-op'ing."""
    import asyncio as _asyncio

    class _Decorate:
        def decorate(self, crud):
            names = [
                "create_entities",
                "update_entities",
                "delete_entities",
                "publish_entities",
                "unpublish_entities",
                "set_publish_status",
                "create_relationships",
            ]
            return dict(zip(names, crud))

    namespace = build_real_exec_namespace(
        entity_api=None,
        relationship_api=None,
        loop=_asyncio.new_event_loop(),
        intercept=_Decorate(),
        tool_cache=None,
        default_language="en",
        entity_repository=None,
        file_repository=None,
    )
    code = """
e1 = e2 = "no-raise"
try:
    get_entity_files("A")
except RuntimeError:
    e1 = "RuntimeError"
try:
    get_file_bytes("f")
except RuntimeError:
    e2 = "RuntimeError"
result = e1 + "|" + e2
"""
    result, error = run_script_sync(code, namespace)
    assert error is None
    assert result == "RuntimeError|RuntimeError"
