from pydantic import BaseModel, Field, PrivateAttr

from uwazi_agent.domain.agent_entity import AgentEntity


class EntityStore(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    entities: list[AgentEntity] = Field(default_factory=list)
    needs_python_agent: bool = False
    # Per-language cache of already-mapped AgentEntity objects, keyed by
    # (shared_id, language). Populated by every entity read so the next
    # ``get_entities_by_shared_ids`` call can return them without re-hitting
    # Uwazi and without re-running the (expensive) EntityMapper pipeline.
    _trim_cache: dict[tuple[str, str], AgentEntity] = PrivateAttr(default_factory=dict)

    def add_entities(self, entities: list[AgentEntity]) -> None:
        existing_ids = {e.shared_id for e in self.entities}
        for entity in entities:
            if entity.shared_id not in existing_ids:
                self.entities.append(entity)
                existing_ids.add(entity.shared_id)
            self._trim_cache[(entity.shared_id, entity.language or "en")] = entity

    def cache_get_many(
        self, shared_ids: list[str], language: str = "en"
    ) -> list[AgentEntity]:
        """Return cached AgentEntity objects for the given ids (cache hits only)."""
        out: list[AgentEntity] = []
        for sid in shared_ids:
            cached = self._trim_cache.get((sid, language))
            if cached is not None:
                out.append(cached)
        return out

    def cache_misses(
        self, shared_ids: list[str], language: str = "en"
    ) -> list[str]:
        """Return the subset of ids that are NOT in the cache for ``language``."""
        return [
            sid
            for sid in shared_ids
            if (sid, language) not in self._trim_cache
        ]

    def invalidate_ids(self, shared_ids: list[str]) -> None:
        """Drop cache entries for the given ids (call after a mutation that may
        change any of them)."""
        sids = set(shared_ids)
        for key in list(self._trim_cache.keys()):
            if key[0] in sids:
                del self._trim_cache[key]
        self.entities = [e for e in self.entities if e.shared_id not in sids]

    def to_context_summary(self) -> str:
        if not self.entities:
            return ""
        templates = sorted({e.template_name for e in self.entities})
        return (
            f"Entity store already has {len(self.entities)} entities loaded "
            f"(templates: {', '.join(templates)}). "
            "Check ``entities`` in your Python code before calling fetch tools — "
            "re-fetching wastes an API call."
        )

    def clear(self) -> None:
        self.entities.clear()
        self._trim_cache.clear()
        self.needs_python_agent = False
