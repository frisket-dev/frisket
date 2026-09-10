"""The ONE injectable project-open seam.

A composition root (an external managed edition, a self-host operator) may need to
decide HOW a project opens: consult a deletion tombstone before handing back
a handle, attach a remote blob store, key both by the tenant the bundle
belongs to. The open tree must not name any of that, but it must not force
those compositions to fork every handler either. ``ProjectOpener`` is that
seam: one callable, injected once at a composition root, threaded to every
site that opens a project.

The division of labour with ``frisket.jobs.queue.claimed_project_location``
is deliberate and total — there is exactly ONE story, not two parallel paths:

* ``claimed_project_location`` decides **WHERE**. It is the sole authority on
  which physical bundle a payload may touch: identity comes from the claimed
  ``ProjectStorageKey`` (reconstructed by the worker from trusted queue
  columns, never from caller-controlled payload JSON) and the directory comes
  from declared registration config (``workspace_root`` +
  ``workspace_root_storage_org_id``). An opener never widens this and is
  never consulted about it.
* ``ProjectOpener`` decides **HOW**. It consumes exactly the location that
  ``claimed_project_location`` produced — the claimed key and that bundle
  path — and nothing else.

So every threaded call site keeps its existing ``claimed_project_location``
call unchanged and only swaps the final ``Project(path)`` construction. With
no opener injected that construction stays a direct, module-local
``Project(path)``, byte-for-byte the current open-tier behaviour.

Fail-closed: an injected opener is a tenant-safety mechanism (tombstones,
per-org blob credentials), so it may only ever be handed a *claimed* identity.
A payload that reaches an opener-bearing handler without a claimed
``ProjectStorageKey`` opens NOTHING — it does not fall back to the
payload-derived path that the unclaimed branch of ``claimed_project_location``
would otherwise allow.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from frisket.project_identity import ProjectStorageKey
from frisket.engine.store import Project

__all__ = [
    "ProjectOpener",
    "claimed_opener_key",
    "open_claimed_project",
    "open_payload_project",
    "require_opener_storage_org_id",
]

#: Open a claimed project. Given the trusted ``ProjectStorageKey`` and the
#: bundle path that ``claimed_project_location`` resolved for it, return an
#: open :class:`~frisket.store.Project`. Raising refuses the open (a deleted
#: project, an identity the opener does not serve).
ProjectOpener = Callable[[ProjectStorageKey, Path], Project]


def claimed_opener_key(payload: object) -> ProjectStorageKey:
    """Return the claimed storage identity, or refuse.

    See the module docstring: an opener is never handed a payload-derived
    identity, because the payload is caller-controlled and the opener is the
    thing enforcing per-tenant policy.
    """
    # Imported lazily: this module sits ABOVE the jobs package so that
    # frisket.executor.action_jobs can name ProjectOpener without importing
    # frisket.jobs (which imports action_jobs right back).
    from frisket.engine.jobs.queue import CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY

    claimed = (
        payload.get(CLAIMED_PROJECT_STORAGE_KEY_PAYLOAD_KEY)
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(claimed, ProjectStorageKey):
        raise ValueError(
            "an injected project opener requires a claimed storage identity; "
            "refusing to open a payload-derived project path"
        )
    return claimed


def open_claimed_project(
    payload: object,
    project_path: str | Path,
    project_opener: ProjectOpener,
) -> Project:
    """Open ``project_path`` through ``project_opener`` under the claimed key.

    ``project_path`` must be the path ``claimed_project_location`` resolved
    for this same payload — the opener chooses HOW to open, never WHERE.
    """
    return project_opener(claimed_opener_key(payload), Path(project_path))


def require_opener_storage_org_id(storage_org_id: object) -> int:
    """Fail-closed org identity for scheduler scans that inject an opener.

    A scheduler root travels with its DECLARED storage-org identity (``cli``'s
    ``SchedulerRoot``). An opener keys every open by that identity, so a
    missing or nonsensical one must refuse the whole scan rather than open
    some tenant's bundles under a bogus key.
    """
    if (
        isinstance(storage_org_id, bool)
        or not isinstance(storage_org_id, int)
        or storage_org_id < 1
    ):
        raise ValueError(
            "a project opener requires this scheduler root's declared positive "
            f"storage_org_id, got {storage_org_id!r}"
        )
    return storage_org_id


def open_payload_project(
    payload: object,
    *,
    workspace_root: str | Path,
    workspace_root_storage_org_id: int | None = None,
    project_opener: ProjectOpener,
) -> tuple[str, Project]:
    """Trusted slug + open project for a handler that HAS an injected opener.

    The one call a threaded handler makes instead of its direct
    ``Project(payload_derived_path)`` open. Both halves of the location come
    from trusted sources — ``claimed_project_location`` with
    ``require_storage_identity=True``, so the slug is the claimed one and the
    directory is this registration's declared root — and only then is the
    opener asked to open it. A handler must not keep reading the payload's own
    ``project_id``/``workspace_root`` on this path: those are caller-controlled
    diagnostics, and an opener exists precisely to enforce per-tenant policy.
    """
    from frisket.engine.jobs.queue import claimed_project_location

    slug, _project_root, project_path = claimed_project_location(
        payload,  # type: ignore[arg-type]
        workspace_root=workspace_root,
        require_storage_identity=True,
        workspace_root_storage_org_id=workspace_root_storage_org_id,
    )
    return slug, open_claimed_project(payload, project_path, project_opener)
