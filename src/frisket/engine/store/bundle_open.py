"""Upgrade known project schemas, fence unknown schemas, then reconcile policy.

Each known upgrade and its digest stamp commit atomically, in schema order.
Current bundles still reconcile standing consent and retention policy.
This leaf uses the facade's connection without importing the facade itself.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any


# The frontend fires hot read endpoints (/sheets, /review/queue) concurrently,
# so two threads can open the same per-project DB at once. Both open-time
# reconciliations below are read-then-write with no CAS: two threads that both
# read "no standing consent for this principal" both mint one, and the bundle
# grows a duplicate policy row per concurrent open. A process-wide per-DB lock
# serializes them. Keyed by resolved db_path.
_OPEN_LOCKS: dict[str, threading.Lock] = {}


_OPEN_LOCKS_GUARD = threading.Lock()


def _open_lock(db_path: Path) -> threading.Lock:
    key = str(Path(db_path).resolve())
    with _OPEN_LOCKS_GUARD:
        lock = _OPEN_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OPEN_LOCKS[key] = lock
        return lock


def open_bundle(project: Any, *, maintenance: bool = False) -> None:
    """Upgrade the known prior schema, fence, then reconcile policy rows."""
    with _open_lock(project.db_path):
        from frisket.upgrades.project import upgrade_project

        upgrade_project(project)
        if maintenance:
            return
        from .purge import require_no_pending_purge

        require_no_pending_purge(project.db)
        _reconcile_open_time_policy(project)
        from frisket.retrieval.recovery import prepare_retrieval_on_open

        prepare_retrieval_on_open(project)


def _reconcile_open_time_policy(project: Any) -> None:
    # Lazy import: execution_routes imports frisket.execution.promises only;
    # this leaf must stay clear of import cycles at module load.
    from frisket.engine.store.execution_routes import ensure_standing_cost_consent

    # §5.1/F2: today's sub-$X no-gate UX as an explicit, auditable, revocable
    # standing consent -- minted for the current installation principal and
    # RECONCILED at every open (an env-knob change mints a successor row;
    # coverage reads the persisted head, never the knob).
    ensure_standing_cost_consent(project)
    project._ensure_retention_policy()
