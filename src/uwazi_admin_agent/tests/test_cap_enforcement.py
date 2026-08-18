"""Isolated unit tests for the pure cap-enforcement decision (Phase 6 DoD).

No mocks, no network — literal manifests + plain assertions.
"""

from datetime import datetime, timezone

import pytest

from uwazi_admin_agent.domain.cap_enforcement import CapExceededError, enforce_cap, touch_set_count
from uwazi_admin_agent.domain.manifest import MigrationManifest, RunStatus
from uwazi_admin_agent.domain.snapshot import EntityIdentity


def _manifest(
    modified: list[str] | None = None,
    deleted: list[str] | None = None,
    created: list[str] | None = None,
) -> MigrationManifest:
    return MigrationManifest(
        run_id="run-1",
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        prompt="d",
        script="x = 1",
        modified=[EntityIdentity(shared_id=sid) for sid in (modified or [])],
        deleted=[EntityIdentity(shared_id=sid) for sid in (deleted or [])],
        created=[EntityIdentity(shared_id=sid) for sid in (created or [])],
        status=RunStatus.PLANNED,
    )


# --- touch_set_count: disjoint sum -----------------------------------------


def test_touch_set_count_is_disjoint_sum() -> None:
    manifest = _manifest(modified=["A", "B"], deleted=["C"], created=["D", "E", "F"])

    assert touch_set_count(manifest) == 6


def test_touch_set_count_empty_manifest_is_zero() -> None:
    assert touch_set_count(_manifest()) == 0


def test_touch_set_count_only_modified() -> None:
    assert touch_set_count(_manifest(modified=["A", "B", "C"])) == 3


def test_touch_set_count_only_created() -> None:
    assert touch_set_count(_manifest(created=["X"])) == 1


# --- enforce_cap: under / at / over the cap ---------------------------------


def test_enforce_cap_under_does_not_raise() -> None:
    # 3 touched, cap 10.
    enforce_cap(_manifest(modified=["A"], deleted=["B"], created=["C"]), cap=10)


def test_enforce_cap_at_exactly_cap_does_not_raise() -> None:
    # 3 touched, cap 3 — equal is allowed (the check is strict-greater-than).
    enforce_cap(_manifest(modified=["A"], deleted=["B"], created=["C"]), cap=3)


def test_enforce_cap_over_raises() -> None:
    with pytest.raises(CapExceededError):
        enforce_cap(_manifest(modified=["A", "B"], deleted=["C"], created=["D"]), cap=3)


def test_enforce_cap_message_names_count_and_cap() -> None:
    with pytest.raises(CapExceededError, match=r"4.*3") as exc_info:
        enforce_cap(_manifest(modified=["A", "B"], deleted=["C"], created=["D"]), cap=3)
    assert "4" in str(exc_info.value)
    assert "3" in str(exc_info.value)


def test_enforce_cap_zero_means_disabled() -> None:
    # cap <= 0 disables enforcement (the only runtime off-switch).
    enforce_cap(_manifest(modified=[f"E{i}" for i in range(100)]), cap=0)


def test_enforce_cap_negative_means_disabled() -> None:
    enforce_cap(_manifest(modified=[f"E{i}" for i in range(100)]), cap=-5)


def test_cap_exceeded_error_is_runtime_error_subclass() -> None:
    assert issubclass(CapExceededError, RuntimeError)
