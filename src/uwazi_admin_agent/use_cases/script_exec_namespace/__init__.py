"""The generated script's sandbox namespace (split into focused modules).

The public surface is unchanged: ``build_exec_namespace``,
``build_real_exec_namespace``, ``build_dry_run_namespace`` and
``run_script_sync`` are re-exported from here.
"""

from __future__ import annotations

from uwazi_agent.use_cases.tools.python_code_executor import _build_sync_crud_functions

from .constants import (
    _BY_IDS_CHUNK,
    _DATEPARSE,
    _HTMLEXTRACT,
    _SAFE_BUILTIN_NAMES,
    _SEARCH_MODES,
    _STDLIB,
    _TEXTMATCH,
    SAFE_BUILTINS,
    NamedNamespace,
)
from .dry_run import (
    _dry_run_full_entities_unwired,
    _dry_run_move_files_helper,
    _dry_run_move_files_parallel_helper,
    _dry_run_moved_files,
    _dry_run_parallel_write_aliases,
    _dry_run_publish_summary,
    _dry_run_query_entities_unwired,
    _dry_run_write_helpers,
)
from .files import (
    _build_get_entity_files_real_helper,
    _build_get_file_bytes_real_helper,
    _build_get_segmentation_real_helper,
    _build_get_url_text_real_helper,
    _build_move_files_real_helper,
    _build_translate_text_real_helper,
)
from .namespace import (
    _captured_stdout,
    build_dry_run_namespace,
    build_real_exec_namespace,
    run_script_sync,
)
from .reads import (
    _real_full_entities_factory,
    _real_sync_query_entities_factory,
    _run_search_mode,
    build_full_entities_view,
    build_search_result_view,
    coerce_search_filters,
)
from .scope import (
    _scoped_entity_reads,
    _scoped_read_wrapper,
    full_mode_usage_error,
    hidden_measured_property,
)
from .selector import build_validation_selector_namespace

__all__ = [
    "_build_sync_crud_functions",
    "_BY_IDS_CHUNK",
    "_SEARCH_MODES",
    "SAFE_BUILTINS",
    "_SAFE_BUILTIN_NAMES",
    "_STDLIB",
    "_DATEPARSE",
    "_TEXTMATCH",
    "_HTMLEXTRACT",
    "NamedNamespace",
    "_build_get_entity_files_real_helper",
    "_build_get_file_bytes_real_helper",
    "_build_get_segmentation_real_helper",
    "_build_get_url_text_real_helper",
    "_build_move_files_real_helper",
    "_build_translate_text_real_helper",
    "_captured_stdout",
    "_dry_run_full_entities_unwired",
    "_dry_run_move_files_helper",
    "_dry_run_move_files_parallel_helper",
    "_dry_run_moved_files",
    "_dry_run_parallel_write_aliases",
    "_dry_run_publish_summary",
    "_dry_run_query_entities_unwired",
    "_dry_run_write_helpers",
    "_real_full_entities_factory",
    "_real_sync_query_entities_factory",
    "_run_search_mode",
    "_scoped_entity_reads",
    "_scoped_read_wrapper",
    "build_dry_run_namespace",
    "build_full_entities_view",
    "build_real_exec_namespace",
    "build_validation_selector_namespace",
    "build_search_result_view",
    "coerce_search_filters",
    "full_mode_usage_error",
    "hidden_measured_property",
    "run_script_sync",
]
