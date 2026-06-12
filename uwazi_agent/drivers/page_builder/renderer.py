from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, Template

from uwazi_agent.drivers.page_builder.registry import BlockRegistry, VibeRegistry


def _chart_color(index: int) -> str:
    """Return a CSS var() string cycling through --chart-1..--chart-6."""
    slot = (index % 6) + 1
    return f"var(--chart-{slot})"


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

    def render(self, vibe: str, blocks: list[dict[str, Any]], vibe_overrides: dict[str, str] | None = None) -> str:
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

        rendered_blocks: list[str] = []
        for block_def in blocks:
            block_type: str = block_def["type"]
            slots: dict[str, Any] = block_def.get("slots", {})

            if block_type == "two_column":
                html = self._render_two_column(vibe, slots)
            else:
                mw_slot = slots.get("max_width", None)
                if mw_slot is not None:
                    if isinstance(mw_slot, int):
                        slots["max_width"] = str(mw_slot)
                template = self._load_template(block_type)
                html = template.render(slots=slots)
            rendered_blocks.append(html)

        tokens_css = self.vibe_registry.get_vibe_tokens(vibe, vibe_overrides)
        body_html = "\n".join(rendered_blocks)
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

    def _load_template(self, block_type: str) -> Template:
        return self._jinja_env.get_template(f"blocks/{block_type}/block.html.j2")

    def _render_two_column(self, vibe: str, slots: dict[str, Any]) -> str:
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
            mw_slot = child_slots.get("max_width", None)
            if mw_slot is not None and isinstance(mw_slot, int):
                child_slots["max_width"] = str(mw_slot)
            template = self._load_template(child_type)
            rendered_children.append(template.render(slots=child_slots))

        template = self._load_template("two_column")
        return template.render(slots=slots, rendered_children=rendered_children)
