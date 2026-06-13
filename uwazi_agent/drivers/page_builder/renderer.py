from pathlib import Path
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from uwazi_agent.drivers.page_builder.registry import BlockRegistry, VibeRegistry


def _chart_color(index: int) -> str:
    """Return a CSS var() string cycling through --chart-1..--chart-6."""
    slot = (index % 6) + 1
    return f"var(--chart-{slot})"


# Strip <style>...</style> blocks from a rendered block fragment.
# Block templates append their CSS as a top-level <style> tag after the
# HTML; we need to lift that CSS into the page-level stylesheet so the
# body contains no <style> tags (which can confuse the React 18 hydration
# walker in Uwazi's MarkdownViewer → html-to-react pipeline).
_STYLE_TAG_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)


def _split_html_and_css(rendered: str) -> tuple[str, str]:
    """Pull every <style> block out of ``rendered`` and return (body, css)."""
    css_parts: list[str] = []
    body = _STYLE_TAG_RE.sub(lambda m: (css_parts.append(m.group(1).strip()) or ""), rendered)
    return body.strip(), "\n".join(p for p in css_parts if p)


SHARED_CSS: str = """
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}
html {
  scroll-behavior: smooth;
}
body {
  font-family: var(--font-body);
  color: var(--color-text);
  background: var(--color-bg);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  overflow-x: hidden;
  font-size: 16px;
  line-height: 1.6;
}
img, svg {
  display: block;
  max-width: 100%;
}
a {
  color: inherit;
  text-decoration: none;
}

/* Shared utilities */
.label {
  display: inline-block;
  font-family: var(--font-body);
  font-size: 0.7rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.18em;
  color: var(--color-primary);
}
"""

SHARED_CSS_SCOPED: str = """
#uwazi-page-root {
  scroll-behavior: smooth;
  font-family: var(--font-body);
  color: var(--color-text);
  background: var(--color-bg);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  overflow-x: hidden;
  font-size: 16px;
  line-height: 1.6;
}
#uwazi-page-root img,
#uwazi-page-root svg {
  display: block;
  max-width: 100%;
}
#uwazi-page-root a {
  color: inherit;
  text-decoration: none;
}
"""


class PageRenderer:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.blocks_dir = base_dir / "blocks"
        self.vibes_dir = base_dir / "vibes"
        self.block_registry = BlockRegistry(self.blocks_dir)
        self.vibe_registry = VibeRegistry(self.vibes_dir)
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(base_dir)),
            autoescape=False,
        )
        self._jinja_env.filters["chart_color"] = _chart_color

    def render_body(
        self,
        vibe: str,
        blocks: list[dict[str, Any]],
        vibe_overrides: dict[str, str] | None = None,
    ) -> str:
        """Validate ``blocks`` and return their concatenated HTML body.

        The result contains **no** ``<style>`` tags — every block's CSS is
        lifted out and returned separately by :meth:`render_css`. The
        rendered body is what should go into the Uwazi page's
        ``metadata.content`` field. Emitting plain HTML (no inline styles)
        is the form Uwazi's React front-end is designed to hydrate.
        """
        rendered_blocks: list[str] = []
        for block_def in blocks:
            block_type: str = block_def["type"]
            slots: dict[str, Any] = block_def.get("slots", {})
            html = self._render_block_html(block_type, slots)
            body, _css = _split_html_and_css(html)
            rendered_blocks.append(body)
        return "\n".join(rendered_blocks)

    def render_css(
        self,
        vibe: str,
        blocks: list[dict[str, Any]],
        vibe_overrides: dict[str, str] | None = None,
    ) -> str:
        """Validate ``blocks`` and return the consolidated page CSS.

        The output bundles:

        * the vibe's design tokens scoped to ``#uwazi-page-root``,
        * the shared global CSS (also scoped),
        * every block's ``<style>`` block, in the order the blocks appear.

        This string is intended for Uwazi's ``metadata.css`` field, which
        Uwazi injects into the document head via its own ``<style>`` tag
        (``PageStyleConnected`` in ``PageViewer.jsx``). Keeping CSS out of
        ``metadata.content`` is what prevents the React 18 hydration error
        triggered by ``<style>`` tags inside the body.
        """
        tokens_css = self.vibe_registry.get_vibe_tokens(vibe, vibe_overrides)
        css_parts: list[str] = [f"#uwazi-page-root {{\n{tokens_css}\n}}\n{SHARED_CSS_SCOPED}".rstrip()]
        for block_def in blocks:
            block_type: str = block_def["type"]
            slots: dict[str, Any] = block_def.get("slots", {})
            html = self._render_block_html(block_type, slots)
            _body, block_css = _split_html_and_css(html)
            if block_css:
                css_parts.append(block_css)
        return "\n\n".join(css_parts)

    def render(
        self,
        vibe: str,
        blocks: list[dict[str, Any]],
        vibe_overrides: dict[str, str] | None = None,
        full_document: bool = True,
    ) -> str:
        errors: list[str] = []

        for idx, block_def in enumerate(blocks):
            block_type = block_def.get("type", "")
            if not block_type:
                errors.append(f"Block at index {idx} is missing 'type' field")
                continue
            slots = block_def.get("slots", {})
            if not isinstance(slots, dict):
                errors.append(f"Block '{block_type}' at index {idx}: 'slots' must be a dict")
                continue
            block_errors = self.block_registry.validate(block_type, slots)
            errors.extend(block_errors)

        if errors:
            error_msg = "Page validation failed:\n  - " + "\n  - ".join(errors)
            raise ValueError(error_msg)

        body_html = self.render_body(vibe, blocks, vibe_overrides=vibe_overrides)
        tokens_css = self.vibe_registry.get_vibe_tokens(vibe, vibe_overrides)

        if full_document:
            return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Uwazi Page</title>
<style>
  :root {{
{tokens_css}
  }}
{SHARED_CSS}
</style>
</head>
<body>
{body_html}
</body>
</html>"""
        else:
            return f"""<style>
  #uwazi-page-root {{
{tokens_css}
  }}
{SHARED_CSS_SCOPED}
</style>
<div id="uwazi-page-root">
{body_html}
</div>"""

    def _render_block_html(self, block_type: str, slots: dict[str, Any]) -> str:
        """Render a single block to its raw HTML (body + inline <style>)."""
        if block_type == "two_column":
            return self._render_two_column(slots)
        mw_slot = slots.get("max_width", None)
        if mw_slot is not None and isinstance(mw_slot, int):
            slots["max_width"] = str(mw_slot)
        template = self._load_template(block_type)
        return template.render(slots=slots)

    def _load_template(self, block_type: str) -> Template:
        return self._jinja_env.get_template(f"blocks/{block_type}/block.html.j2")

    def _render_two_column(self, slots: dict[str, Any]) -> str:
        left_blocks: list[dict[str, Any]] = slots.get("left_blocks", [])
        right_blocks: list[dict[str, Any]] = slots.get("right_blocks", [])

        rendered_children: list[str] = []
        all_children = left_blocks + right_blocks

        for child_def in all_children:
            child_type: str = child_def["type"]
            child_slots: dict[str, Any] = child_def.get("slots", {})
            child_errors = self.block_registry.validate(child_type, child_slots)
            if child_errors:
                raise ValueError("Validation failed in two_column child block:\n  - " + "\n  - ".join(child_errors))
            rendered_children.append(self._render_block_html(child_type, child_slots))

        template = self._load_template("two_column")
        return template.render(slots=slots, rendered_children=rendered_children)
