from typing import Any

from pydantic import BaseModel


def _freeze(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _freeze(value.model_dump())
    if isinstance(value, dict):
        return tuple(sorted((k, _freeze(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    if isinstance(value, set):
        return tuple(sorted(_freeze(v) for v in value))
    return value


class ToolCallCache:
    def __init__(self) -> None:
        self._store: dict[tuple, Any] = {}

    def get(self, tool_name: str, params: dict[str, Any]) -> Any | None:
        key = (tool_name, _freeze(params))
        return self._store.get(key)

    def set(self, tool_name: str, params: dict[str, Any], result: Any) -> None:
        key = (tool_name, _freeze(params))
        self._store[key] = result

    def invalidate_tools(self, tool_names: set[str]) -> None:
        keys_to_remove = [k for k in self._store if k[0] in tool_names]
        for key in keys_to_remove:
            del self._store[key]

    def invalidate_all(self) -> None:
        self._store.clear()
