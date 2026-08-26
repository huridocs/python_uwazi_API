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
from uwazi_admin_agent.adapters.entity_repository_adapter import UwaziEntityRepository
from uwazi_admin_agent.adapters.file_repository_adapter import UwaziFileRepository
from uwazi_admin_agent.adapters.search_probe_adapter import UwaziSearchProbe
from uwazi_admin_agent.configuration import ROOT_PATH, RUNS_PATH
from uwazi_admin_agent.ports.audit_log_port import AuditLogPort
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
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


def build_backup_store(root: Path | None = None) -> BackupStorePort:
    """Return a :class:`FilesystemBackupStore` rooted at ``RUNS_PATH`` (or ``root``)."""
    return FilesystemBackupStore(Path(root) if root is not None else RUNS_PATH)


def build_audit_log(root: Path | None = None) -> AuditLogPort:
    """Return a :class:`JsonlAuditLog` rooted at ``RUNS_PATH`` (or ``root``)."""
    return JsonlAuditLog(Path(root) if root is not None else RUNS_PATH)


def build_runtime(user: str | None = None, password: str | None = None) -> Runtime:
    """Construct the live ports + deps from env (the composition root).

    Explicit ``user``/``password`` win when given; otherwise falls back to
    ``UWAZI_USER``/``UWAZI_PASSWORD`` from the environment (CLI path).
    """
    url = os.environ["UWAZI_URL"]
    user = user if user is not None else os.environ["UWAZI_USER"]
    password = password if password is not None else os.environ["UWAZI_PASSWORD"]

    api = UwaziApiAdapter(user=user, password=password, url=url)
    entity_repository = UwaziEntityRepository(api.client)
    file_repository = UwaziFileRepository(api.client)
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
    )

    revert_use_case = RevertRunUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
        audit_log=audit_log,
        file_repository=file_repository,
    )
    verify_use_case = VerifyRevertUseCase(
        entity_repository=entity_repository,
        backup_store=backup_store,
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
    )
