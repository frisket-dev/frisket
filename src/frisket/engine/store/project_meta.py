"""Meta/manifest store for a project bundle: the manifest.json writer and its
externalized summaries (name/description/sensitive/starred/archived and the
pending-review count), plus the project-level retention and network policies
persisted in the meta table. Free functions over the facade's per-thread
SQLite connection; ``project`` stays duck-typed (``Any``) so this leaf never
re-imports the facade module."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from filelock import FileLock

from frisket.review_predicate import primary_params, primary_where

from .runs import REVIEWABLE_OUTCOMES_SQL
from .schema import FORMAT_VERSION

_log = logging.getLogger(__name__)

PROJECT_SCHEMA_VERSION = "frisket.project.v1"

_ACTIVE_REVIEW_HEAD_JOIN = (
    "LEFT JOIN cell_result_heads active_head "
    "ON active_head.column_id=res.column_id "
    "AND active_head.row_id=res.row_id AND active_head.run_id=res.run_id"
)
_ACTIVE_REVIEW_RESULT_WHERE = (
    "(active_head.run_id IS NOT NULL OR ("
    "c.current_run_id=res.run_id AND NOT EXISTS ("
    "SELECT 1 FROM run_output_generations generation "
    "WHERE generation.column_id=c.id)))"
)


RETENTION_POLICY_META_KEY = "retention_policy"


NETWORK_POLICY_META_KEY = "network_policy"


STORAGE_ID_META_KEY = "storage_id"


SUPPORTED_EVIDENCE_RETENTION_VALUES = ("compactable", "pinned", "materialized")


EVIDENCE_RETENTION_VALUES = set(SUPPORTED_EVIDENCE_RETENTION_VALUES)


DEFAULT_RETENTION_POLICY: dict[str, Any] = {
    "default_evidence": "compactable",
    "pin_evidence_by_default": False,
    "no_compact": False,
}


def _normalize_retention_policy(data: object | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_RETENTION_POLICY)
    if data is None:
        return policy
    if not isinstance(data, dict):
        raise ValueError("retention policy must be an object")
    if "default_evidence" in data:
        default_evidence = str(data["default_evidence"])
        if default_evidence not in EVIDENCE_RETENTION_VALUES:
            raise ValueError(f"unsupported default_evidence: {default_evidence}")
        policy["default_evidence"] = default_evidence
    for key in ("pin_evidence_by_default", "no_compact"):
        if key in data:
            policy[key] = bool(data[key])
    return policy


SUPPORTED_NETWORK_POLICY_MODES = ("inherit", "on", "off")


# The org default is a two-state effective value; "inherit" is only a
# project-level deferral, never an org value.
SUPPORTED_NETWORK_ORG_DEFAULTS = ("on", "off")


DEFAULT_NETWORK_POLICY: dict[str, Any] = {
    "mode": "inherit",
    # Team edition materializes the org default into the bundle (the same
    # reason `sensitive` syncs): the gate reads policy from the Project at
    # validate/dispatch/replay time, where no control-plane handle exists.
    # Local edition never writes it — None resolves to "on".
    "org_default": None,
}


def _normalize_network_policy(data: object | None = None) -> dict[str, Any]:
    policy = dict(DEFAULT_NETWORK_POLICY)
    if data is None:
        return policy
    if not isinstance(data, dict):
        raise ValueError("network policy must be an object")
    if "mode" in data:
        mode = str(data["mode"])
        if mode not in SUPPORTED_NETWORK_POLICY_MODES:
            raise ValueError(f"unsupported network mode: {mode}")
        policy["mode"] = mode
    if "org_default" in data and data["org_default"] is not None:
        org_default = str(data["org_default"])
        if org_default not in SUPPORTED_NETWORK_ORG_DEFAULTS:
            raise ValueError(f"unsupported network org default: {org_default}")
        policy["org_default"] = org_default
    return policy


MANIFEST_NAME = "manifest.json"

# One writer at a time per bundle, across threads AND processes: the server
# and the `frisket worker` container share the data volume and both write
# this file (a run's finalization refreshes pending_review_count while an
# HTTP request can be setting `sensitive`). A cross-process lock is the only
# kind that covers that pair.
MANIFEST_LOCK_NAME = ".manifest.lock"


def manifest_lock(path: Path) -> FileLock:
    """The bundle's manifest write lock (see MANIFEST_LOCK_NAME)."""
    return FileLock(str(path / MANIFEST_LOCK_NAME), timeout=-1)


def write_manifest_atomically(path: Path, manifest: dict[str, Any]) -> None:
    """Publish ``manifest`` at ``path/manifest.json`` as one indivisible step.

    ``write_text`` truncates at open() and only fills the file at close(), so
    every concurrent reader in that window sees ZERO bytes, and a crash inside
    it leaves an empty manifest on disk permanently. JSON has no valid
    prefixes, so those readers do not get a stale-but-sane answer — they get a
    parse error, and readers that treat a parse error as "not a project"
    (server/workspace.py's listing, team/app.py's bundle validity check) turn
    it into a missing project. Written to a sibling temp file and renamed:
    os.replace is atomic within a directory, so a reader sees the whole
    previous manifest or the whole new one. Callers holding
    :func:`manifest_lock` own the fixed temp name for the duration.
    """
    scratch = path / f".{MANIFEST_NAME}.tmp"
    scratch.write_text(json.dumps(manifest, indent=2))
    os.replace(scratch, path / MANIFEST_NAME)


def _write_manifest(project: Any, **updates: Any) -> None:
    """Merge ``updates`` into manifest.json — the one manifest writer.

    The bundle envelope header (format/schema_version/format_version/
    project_id) is ALWAYS stamped, so no write path can leave a manifest
    missing it (refresh_pending_review_summary used to). ``name`` is
    backfilled from meta when absent, matching the retention writer's
    historical behavior.

    This is a read-modify-write, so it holds the bundle's manifest lock for
    the whole of it: unserialized, two writers each merge onto their own
    stale read and the loser's field is silently reverted. One of the fields
    is `sensitive`, the flag that suppresses telemetry — a lost update there
    re-opens automated egress on a project whose owner switched it off.
    """
    with manifest_lock(project.path):
        manifest = _read_manifest(project)
        manifest.update(updates)
        manifest.update(
            {
                "format": "frisket-bundle",
                "schema_version": PROJECT_SCHEMA_VERSION,
                "format_version": FORMAT_VERSION,
                "project_id": project.path.stem,
            }
        )
        storage_id = project.get_meta(STORAGE_ID_META_KEY)
        if storage_id:
            manifest["storage_id"] = storage_id
        if not manifest.get("name"):
            manifest["name"] = project.get_meta("name", project.path.stem)
        if "sensitive" not in manifest:
            # Backfilled on the same terms as storage_id and name above,
            # because a manifest REBUILD goes through here: reopening a bundle
            # whose manifest was lost runs _persist_retention_policy, which
            # writes a manifest with no `sensitive` key at all. Readers that
            # can fall back to meta (project_metadata) survive that; the one
            # that cannot is Workspace.list(), which never opens sqlite and
            # would report the project as not sensitive. Hosted, that False is
            # then written back into the control plane's projects.sensitive,
            # turning a lost manifest into a durably wrong record of which
            # projects are protected.
            stored_sensitive = project.get_meta("sensitive")
            if stored_sensitive is not None:
                manifest["sensitive"] = stored_sensitive == "true"
        write_manifest_atomically(project.path, manifest)


def refresh_pending_review_summary(project: Any) -> int:
    """Recompute the pending-review bundle count (rows/runs still needing
    accept/reject/edit — the same definition runner.review.review_bundle_count
    uses) and persist it into manifest.json's pending_review_count field.

    This is the cheap externalized-summary pattern manifest.json already
    uses for `name`/`retention` (all writers go through _write_manifest):
    Workspace.list() (src/frisket/server/workspace.py) reads this field
    for every project's list-row payload without opening that project's
    sqlite db, which a live per-project COUNT query would require (measured
    ~5-44ms/project depending on OS page-cache state — unacceptable across
    a many-project workspace).

    Call this after any write that can change the count: a run's results
    becoming current (store/runs.py's column-pointer write,
    point_column_at_run), an undo/redo (reverts/reapplies both
    review_state and run pointers), and a typed review.decision mutation."""
    row = project.db.execute(
        f"""
        SELECT COUNT(*) AS count FROM (
            SELECT res.run_id, res.row_id, c.sheet_id
            FROM results res
            JOIN columns c ON c.id = res.column_id
            {_ACTIVE_REVIEW_HEAD_JOIN}
            JOIN runs ON runs.id = res.run_id
            JOIN rows rr ON rr.id = res.row_id AND rr.sheet_id = c.sheet_id
            WHERE res.review_state = 'unreviewed'
              AND res.outcome IN ({REVIEWABLE_OUTCOMES_SQL})
              AND rr.hidden = 0 AND {_ACTIVE_REVIEW_RESULT_WHERE}
              AND {primary_where("c")}
            GROUP BY res.run_id, res.row_id, c.sheet_id
        ) pending_bundles
        """,
        primary_params(),
    ).fetchone()
    count = int(row["count"] if row is not None else 0)
    project._write_manifest(pending_review_count=count)
    return count


def project_metadata(project: Any) -> dict[str, Any]:
    manifest = _read_manifest(project)
    return {
        "id": project.path.stem,
        "name": str(
            manifest.get("name") or project.get_meta("name", project.path.stem)
        ),
        "description": str(
            manifest.get("description") or project.get_meta("description", "") or ""
        ),
        "sensitive": bool(
            manifest.get("sensitive")
            if "sensitive" in manifest
            else project.get_meta("sensitive", "false") == "true"
        ),
        # Shell/Home project flags: additive manifest booleans, default
        # False on legacy bundles that never wrote them.
        "starred": bool(manifest.get("starred", False)),
        "archived": bool(manifest.get("archived", False)),
    }


def set_project_metadata(
    project: Any,
    *,
    name: str | None = None,
    description: str | None = None,
    starred: bool | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    current = project.project_metadata()
    next_name = (
        name.strip() if name is not None else current["name"]
    ) or project.path.stem
    next_description = (
        description.strip() if description is not None else current["description"]
    )
    next_starred = bool(starred) if starred is not None else current["starred"]
    next_archived = bool(archived) if archived is not None else current["archived"]
    project.set_meta("name", next_name)
    project.set_meta("description", next_description)
    project._write_manifest(
        name=next_name,
        description=next_description,
        sensitive=bool(current["sensitive"]),
        starred=next_starred,
        archived=next_archived,
        retention=project.retention_policy(),
    )
    return project.project_metadata()


def set_project_sensitivity(project: Any, sensitive: bool) -> dict[str, Any]:
    """Persist the explicit project-level automated-egress safety flag."""
    if not isinstance(sensitive, bool):
        raise TypeError("sensitive must be a boolean")
    project.set_meta("sensitive", "true" if sensitive else "false")
    project._write_manifest(sensitive=sensitive)
    return project.project_metadata()


# The reading of an UNREADABLE retention policy. Unlike its network-policy
# sibling (whose default is the permissive "on" by a considered tradeoff --
# the off-switch is an explicit choice and a corrupt value there costs a
# blocked call at worst), retention gates a DESTRUCTIVE operation: resetting
# to DEFAULT_RETENTION_POLICY would re-enable compaction of evidence a user
# explicitly pinned, and persisting that reset would destroy the only record
# that they had chosen otherwise. So an unparseable value reads as the most
# protective policy instead, and is NEVER written back -- the stored value
# stays exactly as it is, available for inspection and repair.
UNREADABLE_RETENTION_POLICY: dict[str, Any] = {
    "default_evidence": "pinned",
    "pin_evidence_by_default": True,
    "no_compact": True,
}


def _ensure_retention_policy(project: Any) -> dict[str, Any]:
    raw = project.get_meta(RETENTION_POLICY_META_KEY)
    if raw:
        try:
            return _persist_retention_policy(project, json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            _log.warning(
                "meta key %r does not parse as a retention policy; reading it "
                "as no-compact and leaving the stored value untouched. Set the "
                "policy explicitly to repair it.",
                RETENTION_POLICY_META_KEY,
            )
            return dict(UNREADABLE_RETENTION_POLICY)
    manifest_policy = _read_manifest(project).get("retention")
    return _persist_retention_policy(project, manifest_policy)


def retention_policy(project: Any) -> dict[str, Any]:
    """Project-level v1 retention policy.

    Evidence is compactable by default. Users must explicitly choose
    no-compact or pin-by-default behavior; compaction never flips this based
    on size heuristics. A stored policy that does not parse reads as
    :data:`UNREADABLE_RETENTION_POLICY` (fail closed) and is left on disk
    untouched.
    """
    return project._ensure_retention_policy()


def set_retention_policy(
    project: Any,
    *,
    default_evidence: str | None = None,
    pin_evidence_by_default: bool | None = None,
    no_compact: bool | None = None,
) -> dict[str, Any]:
    policy = project.retention_policy()
    if default_evidence is not None:
        policy["default_evidence"] = default_evidence
    if pin_evidence_by_default is not None:
        policy["pin_evidence_by_default"] = pin_evidence_by_default
    if no_compact is not None:
        policy["no_compact"] = no_compact
    return _persist_retention_policy(project, policy)


def network_policy(project: Any) -> dict[str, Any]:
    """Project-level network policy.

    ``{"mode": "inherit"|"on"|"off", "org_default": "on"|"off"|None}``.
    A safety default, explicitly NOT a guarantee: enforcement is
    app-level (picker filter + dispatch/replay gate); plugins,
    notification delivery and source polling are not covered here.
    """
    raw = project.get_meta(NETWORK_POLICY_META_KEY)
    if raw:
        try:
            return _normalize_network_policy(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            return _persist_network_policy(project, None)
    return _normalize_network_policy()


def set_network_policy(
    project: Any,
    *,
    mode: str | None = None,
    org_default: str | None = None,
    clear_org_default: bool = False,
) -> dict[str, Any]:
    policy = project.network_policy()
    if mode is not None:
        policy["mode"] = mode
    if org_default is not None:
        policy["org_default"] = org_default
    if clear_org_default:
        policy["org_default"] = None
    return _persist_network_policy(project, policy)


def effective_network_policy(project: Any) -> str:
    """The gate's two-state read: ``"on"`` or ``"off"``.

    ``project mode -> org default -> "on"``. The un-configured state is
    ``on`` (turnkey: existing remote-engine workflows keep working; the
    off-switch is an explicit choice)."""
    policy = project.network_policy()
    if policy["mode"] in ("on", "off"):
        return str(policy["mode"])
    return str(policy["org_default"] or "on")


def _read_manifest(project: Any) -> dict[str, Any]:
    manifest_path = project.path / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        data = {}
    if not isinstance(data, dict):
        return {}
    return data


def _persist_retention_policy(project: Any, policy: object | None) -> dict[str, Any]:
    normalized = _normalize_retention_policy(policy)
    project.db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (RETENTION_POLICY_META_KEY, json.dumps(normalized, sort_keys=True)),
    )
    project.db.commit()
    project._write_manifest(retention=normalized)
    return normalized


def _persist_network_policy(project: Any, policy: object | None) -> dict[str, Any]:
    normalized = _normalize_network_policy(policy)
    project.db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (NETWORK_POLICY_META_KEY, json.dumps(normalized, sort_keys=True)),
    )
    project.db.commit()
    project._write_manifest(network=normalized)
    return normalized
