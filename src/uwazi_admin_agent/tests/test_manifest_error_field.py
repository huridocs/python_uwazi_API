"""Isolated unit tests for the manifest's error fields (web error handling).

Pure pydantic round-trip: no I/O, no mocks, no Uwazi instance.
"""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.manifest import MigrationManifest


def _manifest(**overrides: object) -> MigrationManifest:
    base: dict[str, object] = {
        "run_id": "r1",
        "created_at": datetime(2026, 8, 27, tzinfo=timezone.utc),
        "prompt": "merge duplicates of title X",
        "script": "pass",
        "status": "planned",
    }
    base.update(overrides)
    return MigrationManifest.model_validate(base)  # type: ignore[arg-type]


def test_error_fields_default_to_none() -> None:
    manifest = _manifest()
    assert manifest.error is None
    assert manifest.error_step is None


def test_error_fields_round_trip_through_json() -> None:
    manifest = _manifest(error="boom: stack", error_step="execute")
    data = manifest.model_dump()
    assert data["error"] == "boom: stack"
    assert data["error_step"] == "execute"
    restored = MigrationManifest.model_validate_json(manifest.model_dump_json())
    assert restored.error == "boom: stack"
    assert restored.error_step == "execute"


def test_generation_failed_status_persists_through_json() -> None:
    manifest = _manifest(status="generation_failed", error="LLM unavailable", error_step="generate")
    restored = MigrationManifest.model_validate_json(manifest.model_dump_json())
    assert restored.status.value == "generation_failed"
    assert restored.error == "LLM unavailable"
    assert restored.error_step == "generate"


def test_error_fields_clear_to_none_and_round_trip() -> None:
    manifest = _manifest(error="boom", error_step="execute")
    manifest.error = None
    manifest.error_step = None
    restored = MigrationManifest.model_validate_json(manifest.model_dump_json())
    assert restored.error is None
    assert restored.error_step is None
