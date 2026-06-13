"""Unit tests for PageRenderer's body+css split.

The page agent pushes pages to Uwazi by putting HTML in ``metadata.content``
and a stylesheet in ``metadata.css``. The hydration error we saw in
production came from the old behaviour of also emitting inline ``<style>``
tags inside the body — React 18's hydration walker could not reconcile the
DOM tree it produced with what the browser had built. These tests pin down
the new contract: body contains no ``<style>`` tags, css contains all the
selectors the blocks need, and ``render(..., full_document=True)`` (used
only for the LLM's preview) still produces a self-contained HTML file.
"""

import re
from pathlib import Path

import pytest

from uwazi_agent.drivers.page_builder.renderer import PageRenderer, _split_html_and_css


@pytest.fixture
def renderer() -> PageRenderer:
    return PageRenderer(Path("uwazi_agent/drivers/page_builder"))


_HERO_BLOCK: dict = {
    "type": "hero",
    "slots": {
        "title": "Hello, world",
        "subtitle": "Welcome to the archive",
        "category_label": "Archive",
    },
}

_CONTENT_BLOCK: dict = {
    "type": "content",
    "slots": {
        "heading": "About",
        "body_html": "<p>First paragraph.</p><p>Second paragraph.</p>",
    },
}

_TIMELINE_BLOCK: dict = {
    "type": "timeline",
    "slots": {
        "heading": "Recent events",
        "entries": [
            {"date": "2024", "title": "Event A", "description": "First event"},
            {"date": "2025", "title": "Event B", "description": "Second event"},
        ],
    },
}

_CARD_GRID_BLOCK: dict = {
    "type": "card_grid",
    "slots": {
        "heading": "Browse",
        "columns": 2,
        "cards": [
            {"title": "Card 1", "description": "First card"},
            {"title": "Card 2", "description": "Second card"},
        ],
    },
}


def test_split_html_and_css_lifts_inline_style(renderer: PageRenderer) -> None:
    """Direct unit test of the regex helper."""
    sample = (
        '<section class="hero">Hello</section>\n'
        "<style>\n.hero { color: red; padding: 1rem; }\n</style>\n"
        '<section class="card">More</section>\n'
        "<style>\n.card { display: grid; }\n</style>\n"
    )
    body, css = _split_html_and_css(sample)
    assert body, "body should be non-empty"
    assert css, "css should be non-empty"
    assert "<style" not in body.lower()
    assert ".hero" in css
    assert ".card" in css


def test_render_body_has_no_style_tags(renderer: PageRenderer) -> None:
    """The body sent to Uwazi must contain no <style> tags — that's what
    was triggering the React 18 hydration error."""
    blocks = [_HERO_BLOCK, _CONTENT_BLOCK, _TIMELINE_BLOCK, _CARD_GRID_BLOCK]
    body = renderer.render_body(vibe="minimal", blocks=blocks)
    assert body, "body should be non-empty"
    assert "<style" not in body.lower(), f"render_body must not emit <style> tags; got: {body[:200]!r}"
    # Sanity: every block is in the body
    assert 'class="hero"' in body
    assert 'class="content"' in body
    assert 'class="timeline"' in body
    assert 'class="card-grid"' in body


def test_render_css_contains_all_block_selectors(renderer: PageRenderer) -> None:
    blocks = [_HERO_BLOCK, _CONTENT_BLOCK, _TIMELINE_BLOCK, _CARD_GRID_BLOCK]
    css = renderer.render_css(vibe="minimal", blocks=blocks)
    assert css, "css should be non-empty"
    # Design tokens live in the #uwazi-page-root scope
    assert "#uwazi-page-root" in css
    assert "--font-body" in css
    assert "--color-bg" in css
    # Each block's selectors are present
    for selector in (".hero", ".content", ".timeline", ".card-grid"):
        assert selector in css, f"{selector} missing from rendered CSS"


def test_render_full_document_is_self_contained(renderer: PageRenderer) -> None:
    """The preview path (``full_document=True``) is the LLM's sanity check;
    it must still produce a single HTML file that opens cleanly in a
    browser tab. With the new design, the body is style-free and all CSS
    lives in the head's <style> tag — that's a cleaner preview and the
    LLM doesn't have to wade through duplicated selectors anymore."""
    html = renderer.render(vibe="minimal", blocks=[_HERO_BLOCK, _CONTENT_BLOCK])
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html
    # Exactly one <style> tag (in the head); no per-block styles in the body
    assert html.count("<style") == 1
    # The body itself has no <style> tags
    body_match = re.search(r"<body>(.*?)</body>", html, re.DOTALL)
    assert body_match, "preview must have a <body> section"
    body = body_match.group(1)
    assert "<style" not in body.lower()


def test_render_with_unknown_vibe_raises(renderer: PageRenderer) -> None:
    """Vibe validation lives in ``render()`` (the full preview path).
    ``render_body`` and ``render_css`` are also vibe-aware in the sense
    that they refuse to produce output for an unknown vibe — but they
    surface the error from the registry. The full-document path raises
    ValueError with a clear message."""
    with pytest.raises(ValueError, match="Unknown vibe"):
        renderer.render(vibe="does-not-exist", blocks=[_HERO_BLOCK])


def test_two_column_body_has_no_style_tags(renderer: PageRenderer) -> None:
    """two_column renders nested children — make sure their inline styles
    are also lifted out of the body."""
    blocks = [
        {
            "type": "two_column",
            "slots": {
                "left_blocks": [_HERO_BLOCK],
                "right_blocks": [_CONTENT_BLOCK],
            },
        }
    ]
    body = renderer.render_body(vibe="minimal", blocks=blocks)
    assert body
    assert "<style" not in body.lower()


def test_style_extraction_handles_complex_css(renderer: PageRenderer) -> None:
    """The regex used to extract <style> must be greedy over multi-rule
    blocks (e.g. media queries with nested selectors)."""
    sample = (
        '<section class="x">body</section>'
        "<style>\n.x { color: red; }\n.x > p { margin: 0; }\n"
        "@media (max-width: 600px) { .x { font-size: 12px; } }\n"
        "</style>"
    )
    body, css = _split_html_and_css(sample)
    assert "<style" not in body.lower()
    assert ".x" in css
    assert "@media" in css
    assert "(max-width: 600px)" in css


def test_style_extraction_skips_attributes_named_style(renderer: PageRenderer) -> None:
    """We must not eat ``style="..."`` attributes — only full <style> tags."""
    sample = '<div style="color: red">x</div>'
    body, css = _split_html_and_css(sample)
    assert body == sample
    assert css == ""


def test_no_style_tags_in_serialized_rendered_page(renderer: PageRenderer) -> None:
    """End-to-end: what `execute_page_script` actually produces must be
    clean. Body for ``create_page(... html=...)`` and CSS for
    ``create_page(... css=...)``."""
    from uwazi_agent.use_cases.tools.page_script_executor import RenderedPage

    # Mimic the helper that page_script_executor injects into the namespace
    rendered = RenderedPage(
        body=renderer.render_body(vibe="minimal", blocks=[_HERO_BLOCK, _CONTENT_BLOCK]),
        css=renderer.render_css(vibe="minimal", blocks=[_HERO_BLOCK, _CONTENT_BLOCK]),
    )
    assert "<style" not in rendered.body.lower()
    assert ".hero" in rendered.css
    assert ".content" in rendered.css


@pytest.mark.parametrize(
    "block",
    [_HERO_BLOCK, _CONTENT_BLOCK, _TIMELINE_BLOCK, _CARD_GRID_BLOCK],
    ids=["hero", "content", "timeline", "card_grid"],
)
def test_every_block_body_is_style_free(renderer: PageRenderer, block: dict) -> None:
    body = renderer.render_body(vibe="minimal", blocks=[block])
    assert "<style" not in body.lower()
