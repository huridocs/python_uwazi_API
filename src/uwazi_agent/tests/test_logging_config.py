from uwazi_agent.logging_config import truncate_log_message
from uwazi_agent.use_cases.agent_factory import _ToolParamsFormatter


def test_truncate_log_message_keeps_short_messages():
    text = "one\ntwo\nthree"
    assert truncate_log_message(text, max_lines=5) == text


def test_truncate_log_message_truncates_to_max_lines():
    text = "\n".join(f"line {i}" for i in range(1, 11))
    truncated = truncate_log_message(text, max_lines=5)
    lines = truncated.splitlines()
    assert len(lines) == 5
    assert lines[-1] == "... (6 more lines)"


def test_truncate_log_message_empty():
    assert truncate_log_message("", max_lines=5) == ""
    assert truncate_log_message("single", max_lines=0) == "single"


def test_formatter_collapses_multiline_strings():
    params = {"code": "line1\nline2\nline3"}
    result = _ToolParamsFormatter.format(params)
    assert "\n" not in result
    assert result == "code=line1 line2 line3"


def test_formatter_truncates_long_string():
    params = {"code": "x" * 300}
    result = _ToolParamsFormatter.format(params)
    assert len(result) <= 250
    assert result.endswith("…")
