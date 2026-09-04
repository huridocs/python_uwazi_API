"""Pure max-entities cap enforcement (§5 Phase 6).

The touch set is emergent (CRUD-intercepted backup, §2.4), so the cap cannot be
checked ahead of time against a pre-declared scope. Instead the intercept checks
the running touch-set size *after* each mutating op; if it exceeds
:data:`MAX_ENTITIES_PER_RUN` the intercept raises :class:`CapExceededError`
mid-script, which surfaces as a script error and triggers the on-error policy.

The touch set is the set of DISTINCT entities the run has touched: the
union of ``manifest.modified`` + ``manifest.deleted`` + ``manifest.created``
plus the entities whose FILE rows the run deleted
(``manifest.deleted_files`` counts by DISTINCT ENTITY, not by file — one
entity losing three duplicate copies is one touched entity, the same
resource a CRUD op would touch). Rewired from-entities are added to
``modified`` by the intercept (see :func:`decide_backup` for
``create_relationships``), so they are already counted there — no double-count.
The three entity lists are disjoint by construction (first-touch semantics +
created-set tracking in :func:`decide_backup`), so the union's size equals
the historical plain sum for entity-only runs — file-only runs are the only
ones that grow it. Pure: no I/O; the unit-test target named by the Phase 6 DoD
("cap enforcement").
"""

from __future__ import annotations

from uwazi_admin_agent.domain.manifest import MigrationManifest


class CapExceededError(RuntimeError):
    """The run's touch set exceeded the configured max-entities cap."""


def touched_entity_ids(manifest: MigrationManifest) -> set[str]:
    """The DISTINCT shared ids the run has touched (entity writes + file deletes)."""
    touched = {e.shared_id for e in manifest.modified}
    touched |= {e.shared_id for e in manifest.deleted}
    touched |= {e.shared_id for e in manifest.created}
    touched |= {f.shared_id for f in manifest.deleted_files}
    return touched


def touch_set_count(manifest: MigrationManifest) -> int:
    """Return the number of distinct entities the run has touched so far.

    DISTINCT ENTITIES, not files: a run that deletes files from an entity it
    never otherwise touched adds ONE to the count no matter how many file
    rows went away (the cap bounds blast radius per entity, and the three
    entity lists' disjointness makes the union exact).
    """
    return len(touched_entity_ids(manifest))


def enforce_cap(manifest: MigrationManifest, cap: int) -> None:
    """Raise :class:`CapExceededError` iff the touch set exceeds ``cap``.

    Called by the intercept after each op that grows the manifest. A ``cap``
    of ``0`` is interpreted as "no cap" (the only way to disable enforcement
    at runtime — the production cap is always positive).
    """
    if cap <= 0:
        return
    count = touch_set_count(manifest)
    if count > cap:
        raise CapExceededError(
            f"Run touched {count} entities, exceeding the cap of {cap}. "
            "Reverting via the on-error policy (if configured) or halting."
        )
