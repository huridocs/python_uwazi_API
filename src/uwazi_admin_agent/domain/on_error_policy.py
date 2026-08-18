"""Pure on-error policy decision (§5 Phase 6).

When a script raises mid-:class:`ExecuteScriptUseCase`, the operator chooses
what happens next: stop (leave the partial manifest, the operator reverts
later) or stop-and-revert (auto-revert whatever was backed up before the error).
The policy is a value the operator picks per run (``execute --on-error``); the
decision is the pure predicate below, the unit-test target named by the Phase 6
DoD ("per-step error handling with a configurable on-error policy").

Scoped to ``execute`` only: ``generate``/``simulate`` don't mutate real data;
``revert`` errors are partial-restores left as raise-and-surface (operator
inspects + re-runs ``revert`` or ``verify``).
"""

from __future__ import annotations

from enum import Enum

from uwazi_admin_agent.domain.cap_enforcement import touch_set_count
from uwazi_admin_agent.domain.manifest import MigrationManifest


class OnErrorPolicy(str, Enum):
    """What to do when the executed script raises mid-run."""

    STOP = "stop"
    STOP_AND_REVERT = "stop-and-revert"


def should_auto_revert(policy: OnErrorPolicy, manifest: MigrationManifest) -> bool:
    """Pure: decide whether a failed execute should auto-revert the partial run.

    ``STOP`` never auto-reverts. ``STOP_AND_REVERT`` auto-reverts only when the
    run actually touched something (a no-op script error has nothing to
    revert; calling revert would be a pointless status flip).
    """
    return policy == OnErrorPolicy.STOP_AND_REVERT and touch_set_count(manifest) > 0
