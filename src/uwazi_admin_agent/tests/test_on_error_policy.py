"""Isolated unit tests for the pure on-error-policy decision (Phase 6 DoD).

No mocks, no network — literal manifests + plain assertions.
"""

from datetime import datetime, timezone

from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.on_error_policy import OnErrorPolicy, should_auto_revert
from uwazi_admin_agent.domain.snapshot import EntityIdentity


def _manifest(touch_count: int) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        modified=[EntityIdentity(shared_id=f"M{i}") for i in range(touch_count)],
        status=RunStatus.PLANNED,
    )


# --- OnErrorPolicy values ---------------------------------------------------


def test_on_error_policy_values() -> None:
    assert OnErrorPolicy.STOP.value == "stop"
    assert OnErrorPolicy.STOP_AND_REVERT.value == "stop-and-revert"


# --- should_auto_revert: STOP never auto-reverts ----------------------------


def test_stop_never_auto_reverts_even_with_touches() -> None:
    assert should_auto_revert(OnErrorPolicy.STOP, _manifest(touch_count=5)) is False


def test_stop_with_empty_manifest_does_not_revert() -> None:
    assert should_auto_revert(OnErrorPolicy.STOP, _manifest(touch_count=0)) is False


# --- should_auto_revert: STOP_AND_REVERT only when touch set non-empty -------


def test_stop_and_revert_with_touches_auto_reverts() -> None:
    assert should_auto_revert(OnErrorPolicy.STOP_AND_REVERT, _manifest(touch_count=3)) is True


def test_stop_and_revert_with_empty_manifest_does_not_revert() -> None:
    # A no-op script error has nothing to revert; calling revert would be a
    # pointless status flip.
    assert should_auto_revert(OnErrorPolicy.STOP_AND_REVERT, _manifest(touch_count=0)) is False


# --- boundary: exactly one touched entity ----------------------------------


def test_stop_and_revert_with_single_touch_auto_reverts() -> None:
    assert should_auto_revert(OnErrorPolicy.STOP_AND_REVERT, _manifest(touch_count=1)) is True
