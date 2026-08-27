"""Isolated unit tests for the extractor-source validator (pure seam).

Per ``AGENTS.md``: pure functions, literal inputs, plain assertions - no mocks,
no network, no LLM. ``validate_extractor_source`` is the pure gate between the
extractor subagent's emitted source and the ``author_html_extractor`` tool.
"""

from uwazi_admin_agent.use_cases.author_extractor_tool import validate_extractor_source

VALID_EXTRACT = """\
def extract(html):
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
        "def extract(html):\n"
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


def test_two_argument_extract_is_rejected() -> None:
    code = "def extract(html, extra):\n    return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "one argument" in msg


def test_zero_argument_extract_is_rejected() -> None:
    code = "def extract():\n    return None\n"
    msg = validate_extractor_source(code)
    assert msg is not None
    assert "one argument" in msg


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
    msg = validate_extractor_source("undefined_name()\n\ndef extract(html):\n    return None\n")
    assert msg is not None
    assert "exec" in msg.lower()
