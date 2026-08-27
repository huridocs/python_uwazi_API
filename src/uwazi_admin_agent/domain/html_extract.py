"""Pure HTML extraction helpers for entity supporting files (extraction phase).

These functions are bound into the script exec namespace as the ``htmlextract``
namespace (see :mod:`uwazi_admin_agent.use_cases.script_exec_namespace`) so a
generated bulk-edit script can parse an entity's uploaded HTML supporting file
and pull values out of it. They are a curated pure API over stdlib
``html.parser.HTMLParser`` — no bs4/lxml (neither is in ``pyproject.toml`` or
the environment), no I/O, fully deterministic.

Malformed HTML: ``HTMLParser`` is lenient by design; these functions never
raise on string input — they return best-effort results (``""`` / ``[]`` /
``{}`` as appropriate). Non-``str`` input raises ``TypeError`` naturally.
"""

from __future__ import annotations

from html.parser import HTMLParser

# Extensions (lowercased) that mark an uploaded file as HTML even when the
# content_type header is missing or generic.
_HTML_EXTENSIONS: tuple[str, ...] = (".html", ".htm")

_SKIP_CONTENT_TAGS: frozenset[str] = frozenset({"script", "style"})
_BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "table",
        "section",
        "article",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "ul",
        "ol",
        "blockquote",
        "pre",
        "hr",
        "td",
        "th",
        "caption",
    }
)


class _TextExtractor(HTMLParser):
    """Collect visible text, dropping script/style contents and block boundaries."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data:
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


class _TitleExtractor(HTMLParser):
    """Capture the contents of the first ``<title>`` element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title: bool = False
        self._parts: list[str] = []
        self.title: str = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title" and self._in_title:
            self._in_title = False
            if not self.title:
                self.title = "".join(self._parts).strip()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._parts.append(data)


class _TableExtractor(HTMLParser):
    """One entry per ``<table>``: rows of cell texts, colspan-padded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._colspan: int = 1
        self._rowspan_pads: dict[int, int] = {}  # column index -> pending rowspan fillers
        self._skip_depth: int = 0  # script/style inside a cell

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = dict(attrs)
        if tag == "table":
            self._table = []
        elif self._table is not None and tag == "tr":
            self._row = []
            self._rowspan_pads = {}
        elif self._table is not None and self._row is not None and tag in ("td", "th"):
            self._cell_parts = []
            self._colspan = max(1, _int_or(attrd.get("colspan"), 1))
            self._rowspan = max(1, _int_or(attrd.get("rowspan"), 1))
        elif tag in _SKIP_CONTENT_TAGS and self._cell_parts is not None:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_CONTENT_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag in ("td", "th") and self._cell_parts is not None:
            cell = " ".join("".join(self._cell_parts).split())
            pad = self._rowspan_pads.get(len(self._row), 0)  # type: ignore[arg-type]
            for _ in range(pad):
                self._row.append("")  # type: ignore[union-attr]
            for _ in range(self._colspan):
                self._row.append(cell)  # type: ignore[union-attr]
            self._cell_parts = None
            self._colspan = 1
        elif tag == "tr" and self._row is not None:
            self._table.append(self._row)  # type: ignore[union-attr]
            self._row = None
        elif tag == "table" and self._table is not None:
            self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None and self._skip_depth == 0:
            self._cell_parts.append(data)


class _MetaExtractor(HTMLParser):
    """Collect ``<meta>`` tags: key = name or property attribute, value = content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "meta":
            attrd = dict(attrs)
            key = attrd.get("name") or attrd.get("property")
            content = attrd.get("content")
            if key and content is not None:
                self.meta[key] = content


def _int_or(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def html_text(html: str) -> str:
    """All visible text: tags stripped, entities unescaped, script/style dropped,
    whitespace collapsed. ``""`` on empty/malformed input (never raises on str)."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.text()


def html_title(html: str) -> str:
    """Contents of ``<title>``; ``""`` when absent. First title wins."""
    extractor = _TitleExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.title


def html_tables(html: str) -> list[list[list[str]]]:
    """One entry per ``<table>``: rows of cell texts. ``colspan`` repeats the cell
    text so row lengths stay consistent; ``rowspan`` inserts ``""`` placeholders.
    ``[]`` when the document has no tables."""
    extractor = _TableExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.tables


def html_meta(html: str) -> dict[str, str]:
    """``<meta>`` tags keyed by their ``name`` or ``property`` attribute
    (``name`` wins when both are present), value = ``content``. ``{}`` when none."""
    extractor = _MetaExtractor()
    extractor.feed(html)
    extractor.close()
    return extractor.meta


def is_html_ref(ref: object) -> bool:
    """True when a file-ref dict (from ``get_entity_files`` / ``extract_file_refs``)
    is HTML: ``content_type`` startswith ``text/html`` OR ``originalname``
    lowercased ends with ``.html`` / ``.htm``. Never raises on missing keys."""
    if not isinstance(ref, dict):
        return False
    content_type = ref.get("content_type")
    if isinstance(content_type, str) and content_type.lower().startswith("text/html"):
        return True
    originalname = ref.get("originalname")
    return isinstance(originalname, str) and originalname.lower().endswith(_HTML_EXTENSIONS)
