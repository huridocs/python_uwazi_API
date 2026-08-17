"""The composition root for live-wired CLI steps (Phase 5).

Builds the runtime ports/adapters/deps the generate / simulate / execute / revert
step drivers share: a :class:`UwaziApiAdapter` (which is simultaneously the
``entity_api``/``relationship_api``/``thesauri_api``/``template_api`` — it
multi-inherits all the ports), the admin agent's raw
:class:`UwaziEntityRepository` over the same client, a
:class:`FilesystemBackupStore` rooted at ``RUNS_PATH`` (one run = one folder,
per the Phase-5 alignment of ``configuration.py``), an :class:`OllamaAdapter`
LLM, and the :class:`AdminAgentDeps` the generation agent runs against.

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

from uwazi_admin_agent.adapters.backup_store_adapter import FilesystemBackupStore
from uwazi_admin_agent.adapters.entity_repository_adapter import UwaziEntityRepository
from uwazi_admin_agent.configuration import PACKAGE_DIR, RUNS_PATH
from uwazi_admin_agent.ports.backup_store_port import BackupStorePort
from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.use_cases.admin_agent_deps import AdminAgentDeps
from uwazi_agent.adapters.llm.ollama_adapter import OllamaAdapter
from uwazi_agent.adapters.uwazi_api.uwazi_api_adapter import UwaziApiAdapter
from uwazi_agent.ports.entity_api_port import EntityApiPort
from uwazi_agent.ports.llm_port import LlmPort
from uwazi_agent.ports.relationship_api_port import RelationshipApiPort

# ``load_dotenv`` is idempotent; safe to call from every CLI invocation. The
# repo's ``.env`` lives at the project root (``PACKAGE_DIR.parents[1]``).
_ENV_LOADED: bool = load_dotenv(PACKAGE_DIR.parents[1] / ".env")


class Runtime:
    """Live ports + deps shared by the mutating CLI steps."""

    def __init__(
        self,
        entity_api: EntityApiPort,
        relationship_api: RelationshipApiPort | None,
        entity_repository: EntityRepositoryPort,
        backup_store: BackupStorePort,
        llm: LlmPort,
        deps: AdminAgentDeps,
    ) -> None:
        self.entity_api: EntityApiPort = entity_api
        self.relationship_api: RelationshipApiPort | None = relationship_api
        self.entity_repository: EntityRepositoryPort = entity_repository
        self.backup_store: BackupStorePort = backup_store
        self.llm: LlmPort = llm
        self.deps: AdminAgentDeps = deps


def build_backup_store(root: Path | None = None) -> BackupStorePort:
    """Return a :class:`FilesystemBackupStore` rooted at ``RUNS_PATH`` (or ``root``)."""
    return FilesystemBackupStore(Path(root) if root is not None else RUNS_PATH)


def build_runtime() -> Runtime:
    """Construct the live ports + deps from env (the composition root)."""
    url = os.environ["UWAZI_URL"]
    user = os.environ["UWAZI_USER"]
    password = os.environ["UWAZI_PASSWORD"]

    api = UwaziApiAdapter(user=user, password=password, url=url)
    entity_repository = UwaziEntityRepository(api.client)
    backup_store = build_backup_store()
    llm = OllamaAdapter()

    deps = AdminAgentDeps(
        thesauri_api=api,
        template_api=api,
        template_mapper=api.template_mapper,
        entity_api=api,
        relationship_api=api,
    )

    return Runtime(
        entity_api=api,
        relationship_api=api,
        entity_repository=entity_repository,
        backup_store=backup_store,
        llm=llm,
        deps=deps,
    )
