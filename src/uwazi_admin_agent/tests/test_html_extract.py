"""Isolated unit tests for the pure HTML-extraction module.

Per ``AGENTS.md``: pure functions, literal inputs, plain assertions - no mocks,
no network, no real instance.
"""

from uwazi_admin_agent.domain.html_extract import html_meta, html_tables, html_text, html_title, is_html_ref

# --- html_text ----------------------------------------------------------------


def test_html_text_strips_tags_and_collapses_whitespace() -> None:
    html = "<html><body><h1>  Case   42 </h1><p>Decided\non\t2020-01-02.</p></body></html>"
    assert html_text(html) == "Case 42 Decided on 2020-01-02."


def test_html_text_unescapes_entities() -> None:
    assert html_text("<p>A &amp; B &lt;C&gt; &quot;D&quot;</p>") == 'A & B <C> "D"'


def test_html_text_drops_script_and_style_contents() -> None:
    html = (
        "<html><head><script>var x = '<p>not visible</p>';</script>"
        "<style>.a{color:red}</style></head><body><p>visible</p></body></html>"
    )
    assert html_text(html) == "visible"


def test_html_text_empty_and_malformed_never_raise() -> None:
    assert html_text("") == ""
    assert html_text("<<<>>>") == "<<<>>>"  # HTMLParser treats it as text data


# --- html_title ---------------------------------------------------------------


def test_html_title_present() -> None:
    assert html_title("<html><head><title>My  Decision</title></head></html>") == "My  Decision"


def test_html_title_absent() -> None:
    assert html_title("<html><body><p>no title</p></body></html>") == ""
    assert html_title("") == ""


def test_html_title_unescapes_entities() -> None:
    assert html_title("<title>Smith &amp; Co</title>") == "Smith & Co"


# --- html_tables --------------------------------------------------------------


def test_html_tables_simple_two_by_two_with_header() -> None:
    html = "<table><tr><th>Name</th><th>Value</th></tr><tr><td>case_no</td><td>42</td></tr></table>"
    assert html_tables(html) == [[["Name", "Value"], ["case_no", "42"]]]


def test_html_tables_colspan_pads_row_to_consistent_length() -> None:
    html = "<table><tr><td colspan='2'>merged</td></tr><tr><td>a</td><td>b</td></tr></table>"
    assert html_tables(html) == [[["merged", "merged"], ["a", "b"]]]


def test_html_tables_no_tables_returns_empty_list() -> None:
    assert html_tables("<p>no tables here</p>") == []
    assert html_tables("") == []


def test_html_tables_multiple_tables() -> None:
    html = "<table><tr><td>1</td></tr></table><table><tr><td>2</td></tr></table>"
    tables = html_tables(html)
    assert len(tables) == 2
    assert tables[0] == [["1"]]
    assert tables[1] == [["2"]]


def test_html_tables_strips_cell_tags_and_entities() -> None:
    html = "<table><tr><td><b>A</b> &amp; B</td></tr></table>"
    assert html_tables(html) == [[["A & B"]]]


# --- html_meta ----------------------------------------------------------------


def test_html_meta_collects_name_and_property() -> None:
    html = "<head><meta name='description' content='A court decision'><meta property='og:title' content='Case 42'></head>"
    assert html_meta(html) == {"description": "A court decision", "og:title": "Case 42"}


def test_html_meta_no_meta_tags_returns_empty_dict() -> None:
    assert html_meta("<p>none</p>") == {}
    assert html_meta("") == {}


def test_html_meta_missing_content_is_skipped() -> None:
    assert html_meta("<meta name='ghost'>") == {}


# --- is_html_ref --------------------------------------------------------------


def test_is_html_ref_content_type_hit() -> None:
    assert is_html_ref({"content_type": "text/html; charset=utf-8", "originalname": "x.bin"}) is True


def test_is_html_ref_extension_hits() -> None:
    assert is_html_ref({"content_type": "application/octet-stream", "originalname": "page.html"}) is True
    assert is_html_ref({"content_type": "", "originalname": "Page.HTM"}) is True


def test_is_html_ref_miss() -> None:
    assert is_html_ref({"content_type": "application/pdf", "originalname": "doc.pdf"}) is False


def test_is_html_ref_missing_keys_never_raises() -> None:
    assert is_html_ref({}) is False
    assert is_html_ref({"content_type": None, "originalname": None}) is False


def test_is_html_ref_non_dict_is_false() -> None:
    assert is_html_ref(None) is False  # type: ignore[arg-type]
    assert is_html_ref("text/html") is False  # type: ignore[arg-type]
