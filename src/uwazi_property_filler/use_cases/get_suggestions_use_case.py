"""Suggestion gathering + the merge heuristic.

The merge rule is a contract (unit-tested): group by property name; single-value
properties take the highest-confidence value; list-valued properties (whose
suggestions carry list values) take the distinct values ranked by
``(confidence desc, then count of agreeing extensions)``, deduped by a
``json.dumps(value, sort_keys=True)`` key.
"""

from __future__ import annotations

import json
from typing import Any

from uwazi_property_filler.domain.extension_request import ExtensionContext
from uwazi_property_filler.domain.suggestion import Suggestion
from uwazi_property_filler.ports.extension_port import ExtensionPort
from uwazi_property_filler.ports.suggestion_store_port import SuggestionStorePort


def merge_suggestions(suggestions: list[Suggestion]) -> dict[str, Any]:
    groups: dict[str, list[Suggestion]] = {}
    for s in suggestions:
        groups.setdefault(s.property_name, []).append(s)

    result: dict[str, Any] = {}
    for prop, group in groups.items():
        if any(isinstance(s.value, list) for s in group):
            result[prop] = _merge_list(group)
        else:
            result[prop] = _merge_single(group)
    return result


def _merge_single(group: list[Suggestion]) -> Any:
    return max(group, key=lambda s: s.confidence).value


def _merge_list(group: list[Suggestion]) -> list[Any]:
    # Distinct value -> (max confidence, set of agreeing extension ids).
    seen: dict[str, tuple[float, set[str], Any]] = {}
    for s in group:
        for value in s.value if isinstance(s.value, list) else [s.value]:
            key = json.dumps(value, sort_keys=True)
            conf, ext_ids, _ = seen.get(key, (0.0, set(), value))
            seen[key] = (max(conf, s.confidence), ext_ids | {s.source_extension_id}, value)

    ranked = sorted(seen.values(), key=lambda t: (-t[0], -len(t[1])))
    return [value for _, _, value in ranked]


async def get_suggestions(
    ctx: ExtensionContext,
    runners: list[ExtensionPort],
    s: SuggestionStorePort,
    instance_key: str,
) -> list[Suggestion]:
    """Call every enabled suggestion runner, flatten, persist, and return."""
    all_suggestions: list[Suggestion] = []
    for runner in runners:
        all_suggestions.extend(await runner.suggest(ctx))
    await s.save_suggestions(instance_key, ctx.shared_id, all_suggestions)
    return all_suggestions
