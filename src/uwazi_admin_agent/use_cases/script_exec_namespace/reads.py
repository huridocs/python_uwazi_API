"""Sandbox namespace module split out of ``script_exec_namespace`` (behavior preserved)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from loguru import logger

from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_entity_summary import AgentEntitySummary
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.ports.entity_api_port import EntityApiPort

from .constants import _BY_IDS_CHUNK, _SEARCH_MODES
from .scope import filter_ids_to_scope, filter_in_scope_by_template, full_mode_usage_error


def _sync_query_entities_factory(
    dummy_entities: list[AgentEntity],
    scope: set[str],
) -> Any:
    """Build the sync, dummy-scoped ``query_entities`` bound into the namespace.

    In dummy mode discovery is a no-op against Uwazi: the harness already created
    the dummies, so the wrapper returns them (filtered to ``scope``) in the
    shapes the system prompt declares — a result object
    (``.summary`` + ``.examples``, a SimpleNamespace with dict examples) for the search
    modes and a ``list[dict]`` (``model_dump``) for ``by_ids``. Returning dicts matches
    the dict-access idioms the CRUD helpers use (create/update take dict literals).
    This guarantees the script can only ever see dummies during validation,
    regardless of what it searches for.
    """

    def query_entities(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # dict list (by_ids) or SimpleNamespace (search) or str (error)
        _ = language, limit, search_term, template_name, filters, published  # search criteria ignored under scoping
        in_scope = [e for e in dummy_entities if e.shared_id in scope]
        if mode == "by_ids":
            if not shared_ids:
                return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."
            wanted = filter_ids_to_scope(shared_ids, scope)
            by_id = {e.shared_id: e for e in in_scope}
            # Return dicts (model_dump): the system prompt declares by_ids returns
            # "a list of entity dicts", matching the dict-access idioms the CRUD
            # helpers use (create/update take dict literals).
            return [by_id[sid].model_dump() for sid in wanted if sid in by_id]
        if mode in ("by_text", "by_filter", "by_template"):
            # by_template must filter by template_name (mirror real mode) so a
            # script looping by_template over several discovered templates
            # processes each template's dummies once - otherwise every call
            # returns ALL in-scope dummies and the gate false-fails a correct
            # multi-template script. by_text/by_filter stay unfiltered (fuzzy
            # text / property filters can't be simulated on dummies).
            selected = filter_in_scope_by_template(in_scope, template_name) if mode == "by_template" else in_scope
            summary = AgentEntitySummary(
                count=len(selected),
                by_template={},
                sample_titles=[e.title for e in selected[:5]],
                shared_ids=[e.shared_id for e in selected],
            )
            # A result object with `.summary` and `.examples` (dicts). The script
            # reads `summary.shared_ids`, then fetches full dicts via `by_ids`.
            return SimpleNamespace(
                summary=summary,
                examples=[e.model_dump() for e in selected[:5]],
            )
        return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."

    return query_entities


def _sync_full_entities_factory(
    dummy_entities: list[AgentEntity],
    scope: set[str],
) -> Any:
    """Build the sync, dummy-scoped ``query_entities_full`` bound into the namespace.

    Mirrors the real helper's contract for validation: the same three search
    modes, returning the in-scope dummies as FULL entity dicts (``model_dump``)
    — the shape ``by_ids`` returns — so the identical script runs in dummy and
    real mode. Search criteria are ignored under scoping and ``by_template``
    filters by ``template_name``, exactly like the dummy ``query_entities``.
    ``by_ids`` is refused (the real helper does not offer it; ids-only and
    known-id reads belong to ``query_entities``). A ``shared_ids`` argument is
    accepted but IGNORED so the LLM's by_ids habit gets the guidance error
    string instead of a TypeError crash.
    """

    def query_entities_full(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # list[dict] (full entities) or str (error)
        _ = language, limit, search_term, filters, published, shared_ids  # search criteria ignored under scoping
        if mode not in _SEARCH_MODES:
            return full_mode_usage_error(mode)
        in_scope = [e for e in dummy_entities if e.shared_id in scope]
        selected = filter_in_scope_by_template(in_scope, template_name) if mode == "by_template" else in_scope
        return [e.model_dump() for e in selected]

    return query_entities_full


def _real_sync_query_entities_factory(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Build the sync, real-scoped ``query_entities`` that actually calls EntityApiPort.

    Mirrors the dummy wrapper's return shapes exactly (``list[dict]`` for
    ``by_ids``, ``SimpleNamespace(summary, examples)`` for search modes) so the
    identical script runs in both validation and execution. Calls the
    ``EntityApiPort`` async methods via ``loop.run_until_complete``.

    Every mode logs its start (params) and completion (result count + elapsed),
    and ``by_ids`` logs per chunk: it is the one mode whose single call hides a
    long sequential per-entity fetch (one HTTP request per shared_id), and
    those progress lines are the difference between "slow" and "stuck"
    against a remote instance. When a script needs the ENTITIES behind a
    search (not just their ids), prefer the bound ``query_entities_full``:
    it returns them straight from the search instead of re-fetching per id.
    """

    def query_entities(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:
        started = time.monotonic()
        if mode == "by_ids":
            if not shared_ids:
                return "Error: 'by_ids' mode requires `shared_ids` (a non-empty list)."
            target_ids = list(shared_ids[:limit])
            logger.info("script query_entities by_ids: fetching {} entities", len(target_ids))
            dumped: list[dict] = []
            for offset in range(0, len(target_ids), _BY_IDS_CHUNK):
                chunk = target_ids[offset : offset + _BY_IDS_CHUNK]
                entities = loop.run_until_complete(
                    entity_api.get_entities_by_shared_ids(shared_ids=chunk, language=language, limit=len(chunk))
                )
                dumped.extend(e.model_dump() for e in entities)
                logger.info(
                    "script query_entities by_ids: {}/{} ids -> {} entities so far ({:.1f}s)",
                    offset + len(chunk),
                    len(target_ids),
                    len(dumped),
                    time.monotonic() - started,
                )
            return dumped
        if mode not in _SEARCH_MODES:
            return f"Error: unknown mode '{mode}'. Use one of: 'by_text', 'by_filter', 'by_template', 'by_ids'."
        outcome = _run_search_mode(
            entity_api, loop, "query_entities", mode, language, limit, search_term, template_name, filters, published
        )
        if isinstance(outcome, str):
            return outcome
        return build_search_result_view(outcome)

    return query_entities


def _real_full_entities_factory(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
) -> Any:
    """Build the sync, real ``query_entities_full`` that actually calls EntityApiPort.

    The bulk-read counterpart of :func:`_real_sync_query_entities_factory`:
    the same three search modes through the same shared engine
    (:func:`_run_search_mode` — identical traffic by construction), but
    returning the FULL entity dicts (:func:`build_full_entities_view`) instead
    of the summary view, so a script reads a whole search's entities in ONE
    call instead of re-fetching them per-id through ``by_ids`` (one HTTP
    request per entity — unusable at bulk scale). A ``shared_ids`` argument is
    accepted but IGNORED so the LLM's by_ids habit gets the guidance error
    string instead of a TypeError crash.
    """

    def query_entities_full(
        mode: str,
        language: str = "en",
        limit: int = 10000,
        search_term: str | None = None,
        template_name: str | None = None,
        filters: list | None = None,
        published: bool | None = None,
        shared_ids: list[str] | None = None,
    ) -> Any:  # list[dict] (full entities) or str (error)
        _ = shared_ids  # ignored: by_ids reads belong to query_entities
        if mode not in _SEARCH_MODES:
            return full_mode_usage_error(mode)
        outcome = _run_search_mode(
            entity_api, loop, "query_entities_full", mode, language, limit, search_term, template_name, filters, published
        )
        if isinstance(outcome, str):
            return outcome
        return build_full_entities_view(outcome)

    return query_entities_full


def build_search_result_view(result: AgentEntitySearchResult) -> SimpleNamespace:
    """Build the bound ``query_entities`` search-mode return value (real mode).

    Surfaces the FULL list of shared_ids (from ``result._all_entities``) instead
    of the LLM-context-truncated ``result.summary.shared_ids`` (capped to 3 by
    ``UwaziApiAdapter._summarize`` for the agent tool). A generated script that
    discovers entities to group/merge needs every id, not a 3-sample; the
    truncation is an LLM-context concern the bound script helper must not inherit.
    Reading ``result._all_entities`` mirrors ``uwazi_agent``'s own tools
    (``query_entities.py`` etc.); no ``uwazi_api``/``uwazi_agent`` modification.

    Pure: the testable seam extracted from :func:`_real_sync_query_entities_factory`.
    The rest of the summary (count, by_template, sample_titles, note) is
    preserved; ``examples`` stays as the sample dicts (the script fetches full
    dicts via ``by_ids``).
    """
    all_entities: list[AgentEntity] = result._all_entities
    full_ids = [e.shared_id for e in all_entities if e.shared_id]
    summary = result.summary.model_copy(update={"shared_ids": full_ids})
    return SimpleNamespace(summary=summary, examples=[e.model_dump() for e in result.examples])


def build_full_entities_view(result: AgentEntitySearchResult) -> list[dict]:
    """Build the bound ``query_entities_full`` search-mode return value (real mode).

    Returns EVERY entity the search matched — from ``result._all_entities`` —
    as the full ``model_dump`` dicts the script's dict-access idioms expect, the
    SAME shape ``by_ids`` returns. This is the bulk-read fast path: the search
    modes already fetch and map every matching entity into ``_all_entities``
    (one or two paged ``/api/search`` requests), so a script gets the whole set
    in ONE helper call instead of re-fetching each id through ``by_ids`` — one
    HTTP request PER entity, minutes at bulk scale (the ~30-minutes-for-10k
    path this helper replaces). Like :func:`build_search_result_view` it reads
    ``result._all_entities`` (what ``uwazi_agent``'s own tools read); no
    ``uwazi_api``/``uwazi_agent`` modification. Order follows the search result
    (the same order ``summary.shared_ids`` carries), so "first entity"
    semantics are unchanged.

    Pure: the testable seam extracted from :func:`_real_full_entities_factory`.
    """
    return [e.model_dump() for e in result._all_entities]


def coerce_search_filters(filters: list | None) -> list[AgentSearchFilter]:
    """Normalize the ``filters`` argument into a list of :class:`AgentSearchFilter`.

    The bound read helpers accept ``filters`` as either ``AgentSearchFilter``
    instances or plain dicts — the LLM's natural dict-literal habit (e.g.
    ``{"property_name": ..., "values": [...]}``). The adapter reads filter
    fields via attribute access (``f.property_name``), so a raw dict crashes
    with ``AttributeError``; this helper coerces dicts via
    ``AgentSearchFilter(**item)`` so both forms work. Anything that is neither
    a model nor a dict raises :class:`TypeError` with a clear message.

    Pure: the testable seam extracted from :func:`_run_search_mode`.
    """
    if not filters:
        return []
    coerced: list[AgentSearchFilter] = []
    for item in filters:
        if isinstance(item, AgentSearchFilter):
            coerced.append(item)
        elif isinstance(item, dict):
            coerced.append(AgentSearchFilter(**item))
        else:
            raise TypeError(f"filter must be an AgentSearchFilter or a dict, got {type(item).__name__}: {item!r}")
    return coerced


def _run_search_mode(
    entity_api: EntityApiPort,
    loop: asyncio.AbstractEventLoop,
    helper_name: str,
    mode: str,
    language: str,
    limit: int,
    search_term: str | None,
    template_name: str | None,
    filters: list | None,
    published: bool | None,
) -> AgentEntitySearchResult | str:
    """Run one search mode (``by_text``/``by_filter``/``by_template``) against the port.

    The shared engine of the two bound real-mode read helpers —
    ``query_entities`` (summary view) and ``query_entities_full`` (full-dicts
    view) — so their search traffic is IDENTICAL BY CONSTRUCTION: same port
    method, same arguments, same start/completion logging. A missing required
    argument returns the bound helpers' error-string convention; a valid mode
    returns the raw :class:`AgentEntitySearchResult` (its ``_all_entities``
    carries every matched entity; the caller picks the view). Non-search modes
    never reach here — both factories reject them first.
    """
    started = time.monotonic()
    if mode == "by_text":
        if not search_term:
            return "Error: 'by_text' mode requires `search_term`."
        logger.info("script {} by_text: term={!r} template={} limit={}", helper_name, search_term, template_name, limit)
        result = loop.run_until_complete(
            entity_api.search_entities_by_text(
                search_term=search_term, template_name=template_name, language=language, limit=limit
            )
        )
    elif mode == "by_filter":
        if not template_name:
            return "Error: 'by_filter' mode requires `template_name`."
        try:
            coerced_filters = coerce_search_filters(filters)
        except (TypeError, ValueError) as exc:
            return f"Error: invalid filters: {exc}"
        logger.info(
            "script {} by_filter: template={} filters={} limit={}", helper_name, template_name, len(coerced_filters), limit
        )
        result = loop.run_until_complete(
            entity_api.search_entities_by_filter(
                template_name=template_name, filters=coerced_filters, language=language, limit=limit, published=published
            )
        )
    elif mode == "by_template":
        if not template_name:
            return "Error: 'by_template' mode requires `template_name`."
        logger.info("script {} by_template: template={} limit={}", helper_name, template_name, limit)
        result = loop.run_until_complete(
            entity_api.get_entities_by_template(template_name=template_name, language=language, limit=limit)
        )
    else:  # both factories pre-validate the mode; unreachable by a script call
        raise ValueError(f"non-search mode {mode!r} reached _run_search_mode")
    logger.info(
        "script {} {}: {} entities ({:.1f}s)",
        helper_name,
        mode,
        len(result._all_entities),
        time.monotonic() - started,
    )
    return result
