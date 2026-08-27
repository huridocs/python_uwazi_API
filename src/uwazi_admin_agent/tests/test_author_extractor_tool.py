"""Isolated unit tests for the extractor-source validator (pure seam).

Per ``AGENTS.md``: pure functions, literal inputs, plain assertions - no mocks,
no network, no LLM. ``validate_extractor_source`` is the pure gate between the
extractor subagent's emitted source and the ``author_html_extractor`` tool.
"""

from uwazi_admin_agent.use_cases.author_extractor_tool import validate_extractor_source

from uwazi_admin_agent.use_cases.script_exec_namespace import _STDLIB, SAFE_BUILTINS

VALID_EXTRACT = """\
def extract(html, ctx):
    try:
        rows = htmlextract.tables(html)
        for row in rows:
            for i, cell in enumerate(row):
                if cell.strip().lower() == "case number":
                    return {"case_number": row[i + 1]}
        return None
    except Exception:
        return None
"""


def test_valid_extract_passes() -> None:
    assert validate_extractor_source(VALID_EXTRACT) is None


def test_valid_extract_using_re_passes() -> None:
    code = (
        "def extract(html, ctx):\n"
        "    m = re.search(r'Case No\\.?\\s*([0-9]+)', htmlextract.text(html))\n"
        "    return {'case_number': m.group(1)} if m else None\n"
    )
    assert validate_extractor_source(code) is None


def test_import_is_rejected() -> None:
    code = "import re\n" + VALID_EXTRACT
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "import" in msg.lower()


def test_import_from_is_rejected() -> None:
    code = "from html.parser import HTMLParser\n" + VALID_EXTRACT
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "import" in msg.lower()


def test_class_definition_is_rejected() -> None:
    code = "class Extractor:\n    def extract(self, html):\n        return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "class" in msg.lower()


def test_one_argument_extract_is_rejected() -> None:
    code = "def extract(html):\n    return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "two arguments" in msg


def test_varargs_extract_is_rejected() -> None:
    code = "def extract(html, ctx, *rest):\n    return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "two arguments" in msg


def test_zero_argument_extract_is_rejected() -> None:
    code = "def extract():\n    return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "two arguments" in msg


def test_non_callable_extract_is_rejected() -> None:
    msg = validate_extractor_source("extract = 42\n")
    assert msg is not None
    assert "callable" in msg.lower()


def test_missing_extract_is_rejected() -> None:
    msg = validate_extractor_source("def other(html):\n    return None\n")
    assert msg is not None
    assert "callable" in msg.lower()


def test_syntax_error_is_rejected() -> None:
    msg = validate_extractor_source("def extract(html:\n    return None\n")
    assert msg is not None
    assert "parse" in msg.lower()


def test_exec_failure_is_rejected() -> None:
    # Parses fine, contains no imports/classes, but raises when executed.
    msg = validate_extractor_source("undefined_name()\n\ndef extract(html, ctx):\n    return None\n")
    assert msg is not None
    assert "exec" in msg.lower()


def test_extract_selects_row_by_ctx() -> None:
    # Two entities share byte-identical HTML holding a two-row table; each
    # ctx must select its own row. Pure: literal inputs, plain assertions.
    html = (
        "<table>"
        "<tr><th>Title</th><th>Case number</th></tr>"
        "<tr><td>Case A</td><td>2023/111</td></tr>"
        "<tr><td>Case B</td><td>2023/222</td></tr>"
        "</table>"
    )
    code = (
        "def extract(html, ctx):\n"
        "    for table in htmlextract.tables(html):\n"
        "        for row in table:\n"
        "            if row and row[0] == ctx['title']:\n"
        "                return {'case_number': row[1]}\n"
        "    return None\n"
    )
    namespace: dict[str, object] = {"__builtins__": SAFE_BUILTINS, **_STDLIB}
    exec(compile(code, "<extract>", "exec"), namespace)  # noqa: S102 - same sandbox the validator uses
    extract = namespace["extract"]
    assert extract(html, {"shared_id": "a", "title": "Case A", "metadata": {}}) == {"case_number": "2023/111"}
    assert extract(html, {"shared_id": "b", "title": "Case B", "metadata": {}}) == {"case_number": "2023/222"}
    assert extract(html, {"shared_id": "c", "title": "Case C", "metadata": {}}) is None
