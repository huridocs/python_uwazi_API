import builtins

import pytest

from uwazi_admin_agent.use_cases.script_exec_namespace import (
    _STDLIB,
    SAFE_BUILTINS,
    NamedNamespace,
    build_real_exec_namespace,
    coerce_search_filters,
    run_script_sync,
)
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter

# --- filter_ids_to_scope ----------------------------------------------------


# --- assert_ids_in_scope ----------------------------------------------------


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
    hand-rolled pseudo-random. Bound as a curated facade, not the module object."""
    facade = _STDLIB["random"]
    assert callable(facade.randint)
    assert callable(facade.choice)
    assert callable(facade.random)


def test_stdlib_binds_datetime_as_module_not_class() -> None:
    """`datetime` is bound as a MODULE-SHAPED facade so the script uses the standard
    `datetime.datetime.now()` / `datetime.timedelta()` idiom (the LLM's natural
    instinct) and gets `timedelta`/`date`/`time` for free. Pinning this guards the
    live bug where the LLM wrote `datetime.datetime.datetime...` (treating the
    bound name as a class while it was the module, or vice-versa)."""
    facade = _STDLIB["datetime"]
    assert facade.datetime.__name__ == "datetime"
    assert callable(facade.timedelta)
    assert callable(facade.date)


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
    """The embed idiom: a script defines `def extract(html, ctx)` using
    `htmlextract.tables`, runs it over literal (html, ctx) PAIRS — two entities
    with byte-identical HTML but different ctx must extract DIFFERENT rows —
    and sets result. Proves the extraction contract end-to-end in the real
    sandbox namespace."""
    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    code = """
def extract(html, ctx):
    try:
        tables = htmlextract.tables(html)
        for table in tables:
            for row in table:
                if row and row[0] == ctx["title"]:
                    return {"case_number": row[1]}
        return None
    except Exception:
        return None

html = (
    "<table>"
    "<tr><th>Title</th><th>Case number</th></tr>"
    "<tr><td>Case A</td><td>2023/111</td></tr>"
    "<tr><td>Case B</td><td>2023/222</td></tr>"
    "</table>"
)
ctx_a = {"shared_id": "a", "title": "Case A", "metadata": {}}
ctx_b = {"shared_id": "b", "title": "Case B", "metadata": {}}
parsed_a = extract(html, ctx_a)
parsed_b = extract(html, ctx_b)
unmatched = extract(html, {"shared_id": "c", "title": "Case C", "metadata": {}})
result = (
    f"{parsed_a['case_number']}|{parsed_b['case_number']}|"
    f"{'None' if unmatched is None else 'BUG'}"
)
"""
    result, error = run_script_sync(code, ns)
    assert error is None
    assert result == "2023/111|2023/222|None"


def test_run_script_sync_import_html_fails() -> None:
    """`import html` must fail: the static gate rejects any import before exec."""

    ns: dict = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    result, error = run_script_sync("import html\nresult = 'x'\n", ns)
    assert result is None
    assert error is not None and "import" in error.lower()


# --- dummy-scoped file-fetch stubs ----------------------------------------------


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


# --- coerce_search_filters -----------------------------------------------------


def test_coerce_search_filters_accepts_dicts() -> None:
    """Plain dict filters (the LLM's dict-literal habit) coerce to models."""
    coerced = coerce_search_filters([{"property_name": "document_type", "values": ["Request for Provisional Measures"]}])
    assert len(coerced) == 1
    assert isinstance(coerced[0], AgentSearchFilter)
    assert coerced[0].property_name == "document_type"
    assert coerced[0].values == ["Request for Provisional Measures"]


def test_coerce_search_filters_passes_models_through() -> None:
    """Existing AgentSearchFilter instances are returned unchanged."""
    model = AgentSearchFilter(property_name="document_type", values=["X"])
    coerced = coerce_search_filters([model])
    assert coerced == [model]


def test_coerce_search_filters_empty_and_none() -> None:
    """None and empty lists both normalize to an empty list."""
    assert coerce_search_filters(None) == []
    assert coerce_search_filters([]) == []


def test_coerce_search_filters_rejects_non_dict_non_model() -> None:
    """A non-dict, non-model item raises TypeError with a clear message."""
    with pytest.raises(TypeError) as excinfo:
        coerce_search_filters(["document_type"])
    assert "str" in str(excinfo.value)


def test_coerce_search_filters_rejects_missing_required_field() -> None:
    """A dict missing the required ``property_name`` fails pydantic validation."""
    with pytest.raises(ValueError):
        coerce_search_filters([{"values": ["X"]}])


# --- NamedNamespace ----------------------------------------------------------


def test_named_namespace_success_lookups_are_untouched() -> None:
    res = NamedNamespace(summary="ok", examples=[1, 2])

    assert res.summary == "ok"
    assert res.examples == [1, 2]


def test_named_namespace_lists_the_available_attributes() -> None:
    """The failure the operator sees must name the real surface.

    A model-written script used to die on ``res.S`` with a bare
    ``'types.SimpleNamespace' object has no attribute 'S'`` — a dead end for the
    operator AND for the prompt-fix pass that reads the error. The typo must
    instead say what the object actually offers and suggest the closest name.
    """
    res = NamedNamespace(summary="ok", examples=[1, 2])

    with pytest.raises(AttributeError) as excinfo:
        _ = res.S  # noqa: B018 — the attribute access IS the behavior under test

    message = str(excinfo.value)
    assert "'S' does not exist" in message
    assert "Did you mean 'summary'?" in message
    assert "examples" in message


def test_named_namespace_without_a_close_match_lists_every_attribute() -> None:
    res = NamedNamespace(parse=lambda s: s)

    with pytest.raises(AttributeError) as excinfo:
        _ = res.zzzz  # noqa: B018

    assert "Available: parse." in str(excinfo.value)


def test_stdlib_facades_are_named_namespaces() -> None:
    """Every script-facing facade self-diagnoses, not just the result objects."""
    for name in (
        "json",
        "re",
        "collections",
        "itertools",
        "datetime",
        "math",
        "random",
        "htmlextract",
        "textmatch",
        "dateparse",
    ):
        facade = _STDLIB[name]
        assert isinstance(facade, NamedNamespace), name

    with pytest.raises(AttributeError) as excinfo:
        _ = _STDLIB["textmatch"].S  # noqa: B018

    assert "Did you mean 'Scorer'?" in str(excinfo.value), str(excinfo.value)


def test_the_re_facade_binds_the_short_flag_aliases() -> None:
    """``re.I``/``S``/``M``/``X`` are what scripts write; only the long names
    used to be bound, so ``re.sub(..., flags=re.I)`` died and the entity was
    recorded as a skip (whose error text then read like a broken suggestion)."""
    facade = _STDLIB["re"]

    assert facade.I is facade.IGNORECASE
    assert facade.S is facade.DOTALL
    assert facade.M is facade.MULTILINE
    assert facade.X is facade.VERBOSE


def test_run_script_sync_reports_a_typo_with_the_real_surface() -> None:
    """The end-to-end failure a generated script produces stays actionable."""
    namespace: dict = {"res": NamedNamespace(summary="s", examples=[]), "result": None}

    _result, error = run_script_sync("result = res.S.shared_ids\n", namespace)

    assert error is not None
    assert "Did you mean 'summary'?" in error


# --- unique create placeholders ---------------------------------------------


def test_create_placeholders_are_unique_across_calls() -> None:
    """A script that creates one entity per source entity calls the recorder once
    per entity. A per-call index restarted at 0, so every create recorded
    ``dry-created-0``; the review table groups rows by shared_id, so all the
    created entities collapsed into ONE operation whose checkboxes fought each
    other (the same sub-row id appeared in several rows)."""
    from uwazi_admin_agent.domain.suggestion_scoring import records_to_suggestions
    from uwazi_admin_agent.use_cases.script_exec_namespace import _dry_run_write_helpers

    records: list[dict] = []
    create = _dry_run_write_helpers(records)["create_entities"]

    assert create([{"title": "A", "template_name": "T"}]) == [{"shared_id": "dry-created-0", "success": True}]
    assert create([{"title": "B", "template_name": "T"}]) == [{"shared_id": "dry-created-1", "success": True}]
    batch = create([{"title": "C", "template_name": "T"}, {"title": "D", "template_name": "T"}])
    assert [item["shared_id"] for item in batch] == ["dry-created-2", "dry-created-3"]

    rows = records_to_suggestions("run-1", records)
    assert [row.shared_id for row in rows] == ["dry-created-0", "dry-created-1", "dry-created-2", "dry-created-3"]
    assert len({row.shared_id for row in rows}) == len(records)


# --- measured-property guard -------------------------------------------------


def test_strip_measured_property_removes_only_the_measured_key() -> None:
    from uwazi_admin_agent.use_cases.script_exec_namespace.scope import strip_measured_property

    raw = {"shared_id": "e1", "title": "Case", "metadata": {"states": [{"value": "X"}], "judge": "A"}}

    stripped = strip_measured_property(raw, "states")

    assert stripped["metadata"] == {"judge": "A"}
    assert raw["metadata"]["states"] == [{"value": "X"}]  # the caller's dict is untouched


def test_strip_measured_property_handles_the_title_and_absent_keys() -> None:
    from uwazi_admin_agent.use_cases.script_exec_namespace.scope import strip_measured_property

    assert "title" not in strip_measured_property({"shared_id": "e1", "title": "Case"}, "_title")
    raw = {"shared_id": "e1", "metadata": {"judge": "A"}}
    assert strip_measured_property(raw, "states") == raw


def test_measured_property_guard_wraps_every_read_shape() -> None:
    from types import SimpleNamespace

    from uwazi_admin_agent.use_cases.script_exec_namespace.scope import strip_property_from_result

    def entity(shared_id: str) -> dict:
        return {"shared_id": shared_id, "title": shared_id, "metadata": {"states": ["X"], "judge": "A"}}

    listed = strip_property_from_result([entity("a"), entity("b")], "states")
    assert [item["metadata"] for item in listed] == [{"judge": "A"}, {"judge": "A"}]

    searched = strip_property_from_result(
        SimpleNamespace(summary="summary", examples=[entity("a")]),
        "states",
    )
    assert searched.summary == "summary"
    assert searched.examples[0]["metadata"] == {"judge": "A"}

    assert strip_property_from_result("Error: nope", "states") == "Error: nope"


def test_hidden_measured_property_respects_declared_inputs() -> None:
    from uwazi_admin_agent.use_cases.script_exec_namespace.scope import hidden_measured_property

    assert hidden_measured_property("", ["states"]) is None
    assert hidden_measured_property("states") == "states"
    assert hidden_measured_property("states", ["judge"]) == "states"
    assert hidden_measured_property("states", ["states"]) is None
    assert hidden_measured_property("_title", ["title"]) is None
    assert hidden_measured_property("_title") == "_title"
