"""Offline tests for the static script gate (no I/O, no mocks)."""

import pytest

from uwazi_admin_agent.use_cases.script_safety import validate_script_source


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "from os import system",
        "json.dumps.__globals__['__builtins__']",
        "().__class__.__base__.__subclasses__()",
        "getattr(1, 'real')",
        "__builtins__['__import__']('os')",
        "'{0.__class__}'.format(())",
        "x = format(1)",
        "class X: pass",
    ],
)
def test_rejects_introspection_and_imports(code: str) -> None:
    assert validate_script_source(code) is not None


@pytest.mark.parametrize(
    "code",
    [
        "result = [e['title'] for e in query_entities('by_template', template_name='X').examples]",
        'value = f"{dateparse.parse(text)}"',
        "records = {'a': 1}",
        "html = get_url_text(d['shared_id']) or ''",
        "rows = sorted(data.items())",
        "text = '%s' % value",
    ],
)
def test_accepts_normal_scripts(code: str) -> None:
    assert validate_script_source(code) is None


def test_rejects_unparseable_source() -> None:
    message = validate_script_source("def broken(:\n")
    assert message is not None
    assert "parse" in message.lower()


def test_extractor_namespace_also_rejects_introspection() -> None:
    from uwazi_admin_agent.use_cases.extractor_sandbox import build_extractor_namespace

    with pytest.raises(ValueError):
        build_extractor_namespace("def extract(html, ctx):\n    return __import__('os')\n")


def test_extractor_namespace_accepts_a_normal_extractor() -> None:
    from uwazi_admin_agent.use_cases.extractor_sandbox import build_extractor_namespace

    namespace = build_extractor_namespace("def extract(html, ctx):\n    return {'x': 1}\n")
    assert callable(namespace["extract"])
