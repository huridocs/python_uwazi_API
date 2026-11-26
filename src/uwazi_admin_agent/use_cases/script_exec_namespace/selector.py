"""The read-only namespace an LLM-authored validation SELECTOR runs in.

A selector is the wizard's custom test-set picker: the operator describes the
entities ("2024 judgments from Kigali", "modified since March", a list of
shared ids) and the LLM writes a snippet that selects them. It must be able to
do everything a script's DISCOVERY does and nothing else, so this namespace
binds exactly the read helpers plus a deterministic stdlib subset:

- no write helper (``update_entities``, ``create_entities``, ...) is bound;
- no LLM helper (``llm_complete`` / ``llm_map``) is bound;
- ``random`` is NOT bound: two runs of the same selector must pick the same
  entities, or the frozen set (and its score) would not be reproducible.

The snippet sets ``result`` to a list of shared ids, a list of entity dicts, or
a search-result object; the caller normalizes whatever it finds.
"""

from __future__ import annotations

import asyncio
from typing import Any

from uwazi_agent.ports.entity_api_port import EntityApiPort

from .constants import _STDLIB, SAFE_BUILTINS
from .reads import _real_full_entities_factory, _real_sync_query_entities_factory

#: Stdlib facades a selector may use (no ``random``: see the module docstring).
_SELECTOR_STDLIB: dict[str, Any] = {name: module for name, module in _STDLIB.items() if name != "random"}


def build_validation_selector_namespace(entity_api: EntityApiPort, loop: asyncio.AbstractEventLoop) -> dict[str, Any]:
    """The exec namespace for one selector snippet: reads only."""
    return {
        "query_entities": _real_sync_query_entities_factory(entity_api, loop),
        "query_entities_full": _real_full_entities_factory(entity_api, loop),
        **_SELECTOR_STDLIB,
        "__builtins__": SAFE_BUILTINS,
    }


__all__ = ["build_validation_selector_namespace"]
