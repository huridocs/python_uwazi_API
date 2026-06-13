import math
from pathlib import Path
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from uwazi_agent.drivers.page_builder.registry import BlockRegistry, VibeRegistry


def _chart_color(index: int) -> str:
    """Return a CSS var() string cycling through --chart-1..--chart-6."""
    slot = (index % 6) + 1
    return f"var(--chart-{slot})"


def _svg_pie_path(value: float, total: float, start_angle_deg: float, radius: float = 90.0) -> dict[str, str]:
    """Return an SVG sector path descriptor and its end angle in degrees.

    Used by the pie_chart block template so the chart is rendered
    server-side and needs no client-side JavaScript to hydrate.
    """
    if total <= 0 or value <= 0:
        return {"d": "", "end_angle_deg": start_angle_deg}
    angle = (value / total) * 360.0
    end_angle_deg = start_angle_deg + angle
    start_rad = math.radians(start_angle_deg - 90)
    end_rad = math.radians(end_angle_deg - 90)
    x1 = radius * math.cos(start_rad)
    y1 = radius * math.sin(start_rad)
    x2 = radius * math.cos(end_rad)
    y2 = radius * math.sin(end_rad)
    large_arc = 1 if angle > 180 else 0
    d = f"M 0 0 L {x1:.2f} {y1:.2f} A {radius} {radius} 0 {large_arc} 1 {x2:.2f} {y2:.2f} Z"
    return {"d": d, "end_angle_deg": end_angle_deg}


def _svg_donut_path(
    value: float,
    total: float,
    start_angle_deg: float,
    outer_radius: float = 90.0,
    inner_radius: float = 54.0,
) -> dict[str, str]:
    """Return an SVG donut-sector path descriptor and end angle in degrees."""
    if total <= 0 or value <= 0:
        return {"d": "", "end_angle_deg": start_angle_deg}
    angle = (value / total) * 360.0
    end_angle_deg = start_angle_deg + angle
    large_arc = 1 if angle > 180 else 0

    def _pt(r: float, deg: float) -> tuple[float, float]:
        rad = math.radians(deg - 90)
        return r * math.cos(rad), r * math.sin(rad)

    op1 = _pt(outer_radius, start_angle_deg)
    op2 = _pt(outer_radius, end_angle_deg)
    ip1 = _pt(inner_radius, start_angle_deg)
    ip2 = _pt(inner_radius, end_angle_deg)
    d = (
        f"M {op1[0]:.2f} {op1[1]:.2f}"
        f" A {outer_radius} {outer_radius} 0 {large_arc} 1 {op2[0]:.2f} {op2[1]:.2f}"
        f" L {ip2[0]:.2f} {ip2[1]:.2f}"
        f" A {inner_radius} {inner_radius} 0 {large_arc} 0 {ip1[0]:.2f} {ip1[1]:.2f} Z"
    )
    return {"d": d, "end_angle_deg": end_angle_deg}


def _pct(value: float, max_value: float) -> float:
    """Return a percentage clamped to [0, 100]."""
    if max_value <= 0:
        return 0.0
    pct = (value / max_value) * 100.0
    return max(0.0, min(100.0, pct))


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

/* Page-level layout: the root wrapper is full-width, while each block
   keeps its readable inner column. This lets full-bleed blocks (e.g. hero)
   span the entire viewport without breaking out of a narrow parent. */
#uwazi-page-root {
  width: 100%;
  max-width: 100%;
  padding: 0;
  margin: 0;
}

.uwazi-block {
  padding-left: var(--spacing-md);
  padding-right: var(--spacing-md);
}

.uwazi-block__inner {
  width: 100%;
  max-width: 1200px;
  margin-left: auto;
  margin-right: auto;
}

/* A full-bleed block ignores side padding and spans edge-to-edge.
   Its own inner container still centers the content. */
.uwazi-block--full-bleed {
  padding-left: 0;
  padding-right: 0;
}
"""


# Default vibe applied when the caller does not specify one. Lives in
# the renderer module (where the visual themes actually live on disk) so
# the page-builder tool, the agent's schema-store prompt, and any
# other consumer all share the same source of truth.
DEFAULT_VIBE: str = "minimal"


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
        self._jinja_env.filters["svg_pie_path"] = _svg_pie_path
        self._jinja_env.filters["svg_donut_path"] = _svg_donut_path
        self._jinja_env.filters["pct"] = _pct

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

        The blocks are wrapped in ``#uwazi-page-root`` so that the scoped
        design tokens in :meth:`render_css` actually apply to the content.
        """
        rendered_blocks: list[str] = []
        for block_def in blocks:
            block_type: str = block_def["type"]
            slots: dict[str, Any] = block_def.get("slots", {})
            html = self._render_block_html(block_type, slots)
            body, _css = _split_html_and_css(html)
            rendered_blocks.append(body)
        return '<div id="uwazi-page-root">\n' + "\n".join(rendered_blocks) + "\n</div>"

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
        # Uwazi injects metadata.css via PageStyleConnected. The content may
        # be wrapped or scoped depending on the Uwazi viewer, so we emit the
        # design tokens at both :root and #uwazi-page-root to be safe.
        css_parts: list[str] = [
            f":root {{\n{tokens_css}\n}}\n\n#uwazi-page-root {{\n{tokens_css}\n}}\n{SHARED_CSS_SCOPED}".rstrip()
        ]
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
            return f"""{body_html}"""

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
