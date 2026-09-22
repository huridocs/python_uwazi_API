"""In-process extension adapter: wraps a module-level ``extension`` object.

A plugin lives under ``EXTENSIONS_DIR`` as a package (or module) whose top
level exposes an object named ``extension`` with any of ``suggest``/``highlight``/
``display``/``search``/``fill``. Unsupported methods return the empty defaults;
a ``NotImplementedError`` (or any failure) is treated as empty and logged, never
raised to the caller.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import sys
from typing import Any

from loguru import logger

from uwazi_property_filler.adapters.extension_stats import record_error
from uwazi_property_filler.configuration import EXTENSIONS_DIR
from uwazi_property_filler.domain.extension_record import ExtensionRecord
from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.highlight import Highlight
from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.ports.extension_port import ExtensionPort

_EMPTY_MARKDOWN = None
_EMPTY_METADATA = None


def _load_plugin_object(entrypoint: str) -> Any:
    """Import the module at ``entrypoint`` and return its ``extension`` object."""
    root = str(EXTENSIONS_DIR)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module(entrypoint)
    if not hasattr(module, "extension"):
        raise ImportError(f"Plugin module '{entrypoint}' has no top-level 'extension' object")
    return module.extension


class LocalPluginAdapter(ExtensionPort):
    def __init__(self, record: ExtensionRecord):
        self.record = record
        self._obj = _load_plugin_object(record.entrypoint or "")

    async def _call(self, method: str, ctx: ExtensionContext, empty: Any) -> Any:
        fn = getattr(self._obj, method, None)
        if fn is None:
            return empty

        def _run() -> Any:
            result = fn(ctx)
            if inspect.isawaitable(result):
                return asyncio.run(result)
            return result

        try:
            return await asyncio.to_thread(_run)
        except (NotImplementedError, Exception) as exc:  # noqa: BLE001
            logger.error("Local extension '{}' {} failed: {}", self.record.id, method, exc)
            record_error(self.record.id)
            return empty

    async def suggest(self, ctx: ExtensionContext) -> list[Suggestion]:
        return await self._call("suggest", ctx, [])

    async def highlight(self, ctx: ExtensionContext) -> list[Highlight]:
        return await self._call("highlight", ctx, [])

    async def display(self, ctx: ExtensionContext) -> str | None:
        return await self._call("display", ctx, None)

    async def search(self, ctx: ExtensionContext) -> list[dict[str, str]]:
        return await self._call("search", ctx, [])

    async def fill(self, ctx: ExtensionContext) -> dict[str, Any] | None:
        return await self._call("fill", ctx, None)
