"""Read wrappers that hide a run's holdout validation entities from authoring.

The holdout is only real if the GENERATION turn cannot read those entities. The
tools that author a script (``query_entities``, ``peek_entity_files``,
``peek_file_text``, ``peek_segmentation*``, ``benchmark_extractor``, and the
nested extractor subagent) all reach the instance through the ports on
:class:`~uwazi_admin_agent.use_cases.admin_agent_deps.AdminAgentDeps`. Wrapping
the ports ONCE in the composition root covers every tool and every subagent,
including tools that take ids from the LLM, and it keeps the shared
``uwazi_agent`` tools untouched.

Enforcement rules:

- a READ that returns entities drops the excluded ones (and re-derives the
  search summary, so neither the count nor the sample ids mention them);
- a READ addressed at one excluded entity raises :class:`ValidationHoldoutError`
  instead of returning its content: a silent empty answer would let the author
  conclude "this entity has no files" and build the wrong rule;
- writes pass through: these wrappers are only mounted on the AUTHORING
  runtime, which never writes (the apply path keeps the raw ports).

``peek_file_text`` is keyed by a storage filename and ``peek_segmentation`` by a
file ``_id``, neither of which carries an entity identity. They cannot be
filtered here; they are reachable only from a listing of an entity the tools
were allowed to see, so filtering the listing is what keeps them safe.
"""

from __future__ import annotations

from typing import Any

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_agent.domain.agent_entity import AgentEntity
from uwazi_agent.domain.agent_entity_create import AgentEntityCreate
from uwazi_agent.domain.agent_entity_mutation_result import AgentEntityMutationResult
from uwazi_agent.domain.agent_entity_search_result import AgentEntitySearchResult
from uwazi_agent.domain.agent_publish_status import AgentPublishStatus
from uwazi_agent.domain.agent_search_filter import AgentSearchFilter
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_api.domain.segmentation import Segmentation


class ValidationHoldoutError(PermissionError):
    """Raised when authoring asks for an entity the run holds out for scoring."""

    def __init__(self, shared_id: str) -> None:
        super().__init__(
            f"entity {shared_id!r} belongs to this run's validation set and is held out of script generation; "
            "it is only readable by the validation pass"
        )
        self.shared_id = shared_id


def drop_excluded(result: AgentEntitySearchResult, excluded: frozenset[str]) -> AgentEntitySearchResult:
    """One search result with the held-out entities removed (pure).

    Both the visible examples and the private full-entity list are filtered, and
    the summary is re-derived so a "count" or sample id can never disclose a
    held-out entity. A note the API set is kept: it describes the search, not
    the sample.
    """
    if not excluded:
        return result
    entities = [entity for entity in result._all_entities if entity.shared_id not in excluded]
    examples = [entity for entity in result.examples if entity.shared_id not in excluded]
    shared_ids = [sid for sid in result.summary.shared_ids if sid not in excluded]
    by_template = dict(result.summary.by_template)
    sample_titles = [entity.title or "" for entity in examples if entity.title]
    filtered = AgentEntitySearchResult(
        summary=result.summary.model_copy(
            update={
                "count": len(entities),
                "shared_ids": shared_ids,
                "sample_titles": sample_titles[:5],
                "by_template": by_template,
            }
        ),
        examples=examples,
    )
    filtered._all_entities = entities
    return filtered


class ValidationExcludingEntityApi(EntityApiPort):
    """``EntityApiPort`` that hides the run's validation entities from every read."""

    def __init__(self, inner: EntityApiPort, excluded: set[str] | frozenset[str]) -> None:
        self._inner = inner
        self._excluded: frozenset[str] = frozenset(excluded)

    async def get_entities_by_shared_ids(
        self, shared_ids: list[str], language: str, limit: int = 10000
    ) -> list[AgentEntity]:
        allowed = [sid for sid in shared_ids if sid not in self._excluded]
        if not allowed:
            return []
        return await self._inner.get_entities_by_shared_ids(allowed, language, limit)

    async def search_entities_by_text(
        self, search_term: str, template_name: str | None, language: str, limit: int
    ) -> AgentEntitySearchResult:
        result = await self._inner.search_entities_by_text(search_term, template_name, language, limit)
        return drop_excluded(result, self._excluded)

    async def get_entities_by_template(self, template_name: str, language: str, limit: int) -> AgentEntitySearchResult:
        result = await self._inner.get_entities_by_template(template_name, language, limit)
        return drop_excluded(result, self._excluded)

    async def search_entities_by_filter(
        self,
        template_name: str,
        filters: list[AgentSearchFilter],
        language: str,
        limit: int,
        published: bool | None = None,
    ) -> AgentEntitySearchResult:
        result = await self._inner.search_entities_by_filter(template_name, filters, language, limit, published)
        return drop_excluded(result, self._excluded)

    async def create_entities(self, entities: list[AgentEntityCreate], language: str) -> list[AgentEntityMutationResult]:
        return await self._inner.create_entities(entities, language)

    async def update_entities(self, updates: list[AgentEntity], language: str) -> list[AgentEntityMutationResult]:
        return await self._inner.update_entities(updates, language)

    async def delete_entities_by_shared_ids(self, shared_ids: list[str]) -> list[AgentEntityMutationResult]:
        return await self._inner.delete_entities_by_shared_ids(shared_ids)

    async def set_entities_publish_status(self, shared_ids: list[str], published: bool) -> list[AgentEntityMutationResult]:
        return await self._inner.set_entities_publish_status(shared_ids, published)

    async def get_publish_status(self, shared_ids: list[str], language: str) -> list[AgentPublishStatus]:
        allowed = [sid for sid in shared_ids if sid not in self._excluded]
        if not allowed:
            return []
        return await self._inner.get_publish_status(allowed, language)


class ValidationExcludingEntityRepository(EntityRepositoryPort):
    """Raw reads refuse a held-out entity; writes pass through untouched."""

    def __init__(self, inner: EntityRepositoryPort, excluded: set[str] | frozenset[str]) -> None:
        self._inner = inner
        self._excluded: frozenset[str] = frozenset(excluded)

    async def get_raw_by_shared_id(self, shared_id: str, language: str | None = None) -> dict[str, Any]:
        if shared_id in self._excluded:
            raise ValidationHoldoutError(shared_id)
        return await self._inner.get_raw_by_shared_id(shared_id, language)

    async def get_raw_by_internal_id(self, internal_id: str) -> dict[str, Any]:
        return await self._inner.get_raw_by_internal_id(internal_id)

    async def save_raw(self, raw: dict[str, Any]) -> None:
        return await self._inner.save_raw(raw)

    async def save_raw_properties(self, payload: dict[str, Any]) -> None:
        return await self._inner.save_raw_properties(payload)

    async def update_many_properties(self, shared_ids: list[str], metadata: dict[str, Any], language: str) -> list[str]:
        return await self._inner.update_many_properties(shared_ids, metadata, language)

    async def create_raw(self, raw: dict[str, Any]) -> str:
        return await self._inner.create_raw(raw)

    async def delete_by_shared_id(self, shared_id: str) -> None:
        return await self._inner.delete_by_shared_id(shared_id)


class ValidationExcludingSegmentationRepository(SegmentationRepositoryPort):
    """Segmentation by entity refuses a held-out entity; by file id passes through."""

    def __init__(self, inner: SegmentationRepositoryPort, excluded: set[str] | frozenset[str]) -> None:
        self._inner = inner
        self._excluded: frozenset[str] = frozenset(excluded)

    async def get_by_file_id(self, file_id: str) -> Segmentation:
        return await self._inner.get_by_file_id(file_id)

    async def get_by_shared_id(self, shared_id: str, language: str) -> Segmentation:
        if shared_id in self._excluded:
            raise ValidationHoldoutError(shared_id)
        return await self._inner.get_by_shared_id(shared_id, language)


__all__ = [
    "ValidationExcludingEntityApi",
    "ValidationExcludingEntityRepository",
    "ValidationExcludingSegmentationRepository",
    "ValidationHoldoutError",
    "drop_excluded",
]
