"""The composition root for live-wired CLI steps (Phase 5).

Builds the runtime ports/adapters/deps the generate / simulate / execute / revert
step drivers share: a :class:`UwaziApiAdapter` (which is simultaneously the
``entity_api``/``relationship_api``/``thesauri_api``/``template_api`` — it
multi-inherits all the ports), the admin agent's raw
:class:`UwaziEntityRepository` over the same client, a
:class:`FilesystemBackupStore` rooted at ``RUNS_PATH`` (one run = one folder,
per the Phase-5 alignment of ``configuration.py``), a :class:`JsonlAuditLog`
over the same root (Phase 6 — every write is audited), an :class:`OllamaAdapter`
LLM, and the :class:`AdminAgentDeps` the generation agent runs against.

Phase 6 also pre-builds a :class:`RevertRunUseCase` (wired with the audit log so
auto-revert records too) and a :class:`VerifyRevertUseCase` (post-revert
verification), both held on the :class:`Runtime` so the step drivers can wire
them into execute/revert/verify without re-constructing.

The runtime also wires the persistent cross-task file/entity cache
(:func:`build_file_cache`, rooted under ``data/file_cache/<instance>/``): both
repositories are wrapped with read-through decorators at this seam so every
consumer shares the cache without any contract change, and the store is held
on the :class:`Runtime` for the step drivers to wire as the stats/
invalidation port.

Live wiring reads ``UWAZI_URL``/``UWAZI_USER``/``UWAZI_PASSWORD`` from the
environment (mirrors ``uwazi_agent``'s ``run_agent`` driver). It is **not**
unit-tested — it constructs real adapters that touch the network; the step
drivers that consume it are validated via the simulation run.

``list-runs`` and ``inspect-run`` do not need a live Uwazi instance; they use
:func:`build_backup_store` alone (pure filesystem).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from uwazi_admin_agent.adapters.audit_log_adapter import JsonlAuditLog
from uwazi_admin_agent.adapters.backup_store_adapter import FilesystemBackupStore
from uwazi_admin_agent.adapters.cached_entity_repository import CachedEntityRepository
from uwazi_admin_agent.adapters.cached_file_repository import CachedFileRepository
from uwazi_admin_agent.adapters.entity_repository_adapter import UwaziEntityRepository
from uwazi_admin_agent.adapters.file_cache_store import FileCacheStore
from uwazi_admin_agent.adapters.file_repository_adapter import UwaziFileRepository
from uwazi_admin_agent.adapters.search_probe_adapter import UwaziSearchProbe
from uwazi_admin_agent.adapters.template_property_adapter import UwaziTemplatePropertyLookup
from uwazi_admin_agent.configuration import (
    DUMMY_LANGUAGE,
    ENTITY_CACHE_TTL_SECONDS,
    FILE_CACHE_DIR,
    FILE_CACHE_ENABLED,
    FILE_CACHE_EVICT_SCAN_INTERVAL,
    FILE_CACHE_MAX_BYTES,
    ROOT_PATH,
    RUNS_PATH,
)
from uwazi_admin_agent.domain.file_cache import instance_dir_name
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_admin_agent.use_cases.dry_run_script_use_case import DryRunScriptUseCase
from uwazi_admin_agent.use_cases.revert_run_use_case import RevertRunUseCase
from uwazi_admin_agent.use_cases.verify_revert_use_case import VerifyRevertUseCase
from uwazi_agent.adapters.llm.ollama_adapter import OllamaAdapter
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort

# ``load_dotenv`` is idempotent; safe to call from every CLI invocation.
_ENV_LOADED: bool = load_dotenv(ROOT_PATH / ".env")


class Runtime:
    """Live ports + deps shared by the mutating CLI steps."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        relationship_api: RelationshipApiPort | None,
        entity_repository: EntityRepositoryPort,
        file_repository: FileRepositoryPort,
        backup_store: BackupStorePort,
        audit_log: AuditLogPort,
        llm: LlmPort,
        deps: AdminAgentDeps,
        revert_use_case: RevertRunUseCase,
        verify_use_case: VerifyRevertUseCase,
        search_probe: SearchProbePort,
        file_cache: FileCacheStore | None,
    ) -> None:
        self.entity_api: EntityApiPort = entity_api
        self.relationship_api: RelationshipApiPort | None = relationship_api
        self.entity_repository: EntityRepositoryPort = entity_repository
        self.file_repository: FileRepositoryPort = file_repository
        self.backup_store: BackupStorePort = backup_store
        self.audit_log: AuditLogPort = audit_log
        self.llm: LlmPort = llm
        self.deps: AdminAgentDeps = deps
        self.revert_use_case: RevertRunUseCase = revert_use_case
        self.verify_use_case: VerifyRevertUseCase = verify_use_case
        self.search_probe: SearchProbePort = search_probe
        # The persistent file/entity cache (None = disabled). Held here so the
        # step drivers can wire it into their use cases as the stats/invalidation
        # port — the counters live on the store both decorators bump.
        self.file_cache: FileCacheStore | None = file_cache


def build_backup_store(root: Path | None = None) -> BackupStorePort:
    """Return a :class:`FilesystemBackupStore` rooted at ``RUNS_PATH`` (or ``root``)."""
    return FilesystemBackupStore(Path(root) if root is not None else RUNS_PATH)


def build_audit_log(root: Path | None = None) -> AuditLogPort:
    """Return a :class:`JsonlAuditLog` rooted at ``RUNS_PATH`` (or ``root``)."""
    return JsonlAuditLog(Path(root) if root is not None else RUNS_PATH)


def build_file_cache(url: str) -> FileCacheStore | None:
    """Build the persistent cross-task cache for the instance at ``url`` (None = off).

    Rooted under ``FILE_CACHE_DIR`` (``data/file_cache/``) so it survives across
    processes, tasks, and runs, and namespaced per instance URL (the same
    sharedId on two instances means different data). Both CLI steps and the web
    driver build their ports through :func:`build_runtime`, so wrapping the
    repositories here covers every consumer (sandbox helpers, peek tools, dry
    run, execute + backup intercept, revert/verify) with zero contract changes.
    """
    if not _cache_enabled():
        return None
    return FileCacheStore(
        root=FILE_CACHE_DIR / instance_dir_name(url),
        max_bytes=FILE_CACHE_MAX_BYTES,
        ttl_seconds=ENTITY_CACHE_TTL_SECONDS,
        evict_scan_interval=FILE_CACHE_EVICT_SCAN_INTERVAL,
    )


def _cache_enabled() -> bool:
    """``FILE_CACHE_ENABLED`` unless the UWAZI_ADMIN_FILE_CACHE env var overrides it."""
    raw = os.environ.get("UWAZI_ADMIN_FILE_CACHE")
    if raw is None:
        return FILE_CACHE_ENABLED
    return raw.strip().lower() not in ("0", "false", "no", "off")


def build_runtime(user: str | None = None, password: str | None = None) -> Runtime:
    """Construct the live ports + deps from env (the composition root).

    Explicit ``user``/``password`` win when given; otherwise falls back to
    ``UWAZI_USER``/``UWAZI_PASSWORD`` from the environment (CLI path).
    """
    url = os.environ["UWAZI_URL"]
    user = user if user is not None else os.environ["UWAZI_USER"]
    password = password if password is not None else os.environ["UWAZI_PASSWORD"]

    api = UwaziApiAdapter(user=user, password=password, url=url)
    file_cache = build_file_cache(url)
    base_entity_repository = UwaziEntityRepository(api.client)
    base_file_repository = UwaziFileRepository(api.client)
    # Wrap ONCE at this seam so every consumer transparently shares the
    # persistent cache; when it is disabled the plain adapters are used and
    # nothing downstream can tell the difference.
    entity_repository: EntityRepositoryPort = (
        CachedEntityRepository(base_entity_repository, file_cache) if file_cache is not None else base_entity_repository
    )
    file_repository: FileRepositoryPort = (
        CachedFileRepository(base_file_repository, file_cache) if file_cache is not None else base_file_repository
    )
    template_property_lookup = UwaziTemplatePropertyLookup(api.client)
    search_probe = UwaziSearchProbe(api.client)
    backup_store = build_backup_store()
    audit_log = build_audit_log()
    llm = OllamaAdapter()

    deps = AdminAgentDeps(
        thesauri_api=api,
        template_api=api,
        template_mapper=api.template_mapper,
        entity_api=api,
        relationship_api=api,
        search_probe=search_probe,
        file_repository=file_repository,
        dry_run_use_case=DryRunScriptUseCase(
            entity_api=api,
            entity_repository=entity_repository,
            file_repository=file_repository,
            default_language=DUMMY_LANGUAGE,
            cache_stats=file_cache,
        ),
    )

    revert_use_case = RevertRunUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        audit_log=audit_log,
        file_repository=file_repository,
        template_property_lookup=template_property_lookup,
        # Re-upload targets' cached raws are dropped after the file restores
        # (a re-upload mutates the files collection, not the entity row).
        cache_control=file_cache,
    )
    verify_use_case = VerifyRevertUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        # Deleted-file restores are verified by CONTENT (sha256 of the restored
        # bytes vs the captured bytes), which needs byte fetches.
        file_repository=file_repository,
        # Invalidate-then-refetch: verification never reads through a cache
        # entry our own writes could have made stale.
        cache_control=file_cache,
    )

    return Runtime(
        entity_api=api,
        relationship_api=api,
        entity_repository=entity_repository,
        file_repository=file_repository,
        backup_store=backup_store,
        audit_log=audit_log,
        llm=llm,
        deps=deps,
        revert_use_case=revert_use_case,
        verify_use_case=verify_use_case,
        search_probe=search_probe,
        file_cache=file_cache,
    )
