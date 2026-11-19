"""Web driver module split out of the original ``run_service`` (behavior preserved)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from uwazi_admin_agent.domain.drift import DriftStateRow
from uwazi_admin_agent.drivers.runtime import build_runtime
from uwazi_admin_agent.use_cases import drift_service

from .wiring import build_suggestion_store

DRIFT_PROBE_LIMIT: int = drift_service.DRIFT_PROBE_LIMIT


_drift_detail = drift_service.drift_detail


def sync_drift_baseline(
    url: str,
    run_id: str,
    user: str,
    password: str,
) -> int:
    """Record ``(sharedId, language) -> editDate`` for the run's template snapshot."""
    runtime = build_runtime(user=user, password=password, url=url)
    return drift_service.sync_drift_baseline(
        build_suggestion_store(url),
        runtime.drift_probe,
        _template_id_resolver(runtime),
        run_id,
    )


def get_drift_state(url: str, run_id: str) -> DriftStateRow | None:
    """The run's last-check outcome (the banner reads this)."""
    return build_suggestion_store(url).drift_state(run_id)


def check_drift(
    url: str,
    run_id: str,
    user: str,
    password: str,
    by: str = "auto",
) -> DriftStateRow:
    """Check whether Uwazi drifted since the baseline; persist + return the verdict."""
    runtime = build_runtime(user=user, password=password, url=url)
    return drift_service.check_drift(
        build_suggestion_store(url),
        runtime.drift_probe,
        runtime.backup_store,
        runtime.entity_repository,
        runtime.file_cache,
        _template_id_resolver(runtime),
        run_id,
        by=by,
    )


def refresh_baseline_after_run(url: str, run_id: str, user: str, password: str) -> None:
    """After an apply: re-sync the baseline for the run's template (best effort)."""
    runtime = build_runtime(user=user, password=password, url=url)
    drift_service.refresh_baseline_after_run(
        build_suggestion_store(url),
        runtime.drift_probe,
        _template_id_resolver(runtime),
        run_id,
    )


def _template_id(runtime: Any, template_name: str | None) -> str | None:
    """Resolve a run's template name to its id (None = unresolvable/untemplated)."""
    if not template_name:
        return None
    try:
        template = runtime.entity_api.client.templates.get_by_name(template_name)
    except Exception as exc:  # noqa: BLE001 — an unresolvable template disables the probe
        logger.warning("could not resolve template {}: {}", template_name, exc)
        return None
    return template.id if template is not None else None


def _template_id_resolver(runtime: Any) -> Callable[[str | None], str | None]:
    """Bind the runtime's template lookup as the drift use case's resolver."""
    return lambda name: _template_id(runtime, name)
