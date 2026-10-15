"""The admin agent's pydantic-ai deps (Phase 3).

Extends :class:`UwaziAgentToolsDependencies` (reused from ``uwazi_agent``) with
the raw ports the generation-time sampling tools need (files, segmentation,
external URLs) plus the extractor subagent handle.

Subclassing (rather than editing the shared ``UwaziAgentToolsDependencies``)
keeps the admin agent's extension owned by the admin agent. The reused
``query_entities`` tool is typed ``RunContext[UwaziAgentToolsDependencies]``; at
runtime it receives an ``AdminAgentDeps`` and accesses only parent fields, which
is sound because ``AdminAgentDeps`` *is-a* ``UwaziAgentToolsDependencies``
(verified end-to-end with pydantic-ai's TestModel).
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from uwazi_admin_agent.ports.entity_repository_port import EntityRepositoryPort
from uwazi_admin_agent.ports.file_repository_port import FileRepositoryPort
from uwazi_admin_agent.ports.search_probe_port import SearchProbePort
from uwazi_admin_agent.ports.segmentation_repository_port import SegmentationRepositoryPort
from uwazi_admin_agent.ports.url_fetcher_port import UrlFetcherPort
from uwazi_agent.use_cases.tools.dependencies import UwaziAgentToolsDependencies


class AdminAgentDeps(UwaziAgentToolsDependencies):
    """``UwaziAgentToolsDependencies`` + raw ports + the extractor subagent."""

    entity_repository: EntityRepositoryPort | None = Field(
        default=None,
        description="Raw entity repository for planning-time reads. Set by build_runtime.",
    )
    search_probe: SearchProbePort | None = Field(
        default=None,
        description=(
            "ES search probe for generation-time discovery sampling. When None the "
            "sampling degrades gracefully (backward-compatible; tests stay green)."
        ),
    )
    file_repository: FileRepositoryPort | None = Field(
        default=None,
        description=(
            "Raw file repository for generation-time HTML sampling (peek_file_text) "
            "and the bound get_file_bytes helper. Set by build_runtime."
        ),
    )
    segmentation_repository: SegmentationRepositoryPort | None = Field(
        default=None,
        description=(
            "Raw segmentation repository for generation-time document-text reading "
            "(peek_segmentation / peek_segmentation_by_entity). Set by build_runtime."
        ),
    )
    url_fetcher: UrlFetcherPort | None = Field(
        default=None,
        description=(
            "External URL fetcher for source-page HTML sampling (peek_url_text) and "
            "the bound get_url_text helper. Set by build_runtime."
        ),
    )
    extractor_agent: Any | None = Field(
        default=None,
        description=(
            "The extractor subagent (pydantic-ai Agent), built once by "
            "GenerateScriptUseCase and called by the author_html_extractor tool."
        ),
    )
