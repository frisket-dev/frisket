"""Fold the stored network on/off setting into Allowed services.

A project whose network setting was off (its own mode, or an organization
default of off it inherited) moves to Approved services unless its Allowed
services is already stricter; a project that was on keeps its Allowed
services. The network key is removed in the same transaction, and the
manifest is rewritten from the Allowed services value actually stored.

On Frisket Cloud, Approved services allows Frisket's own processing, which
network off refused, so a network-off Cloud project stores This machine only
even though Cloud does not offer it for selection: nothing loosens, and its
owner chooses an offered option, with the usual confirmation, to go further.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.store.project_meta import (
    MANIFEST_NAME,
    manifest_lock,
    write_manifest_atomically,
)
from frisket.engine.store.transactions import write_transaction
from frisket.service_access import workspace_edition

NETWORK_POLICY_META_KEY = "network_policy"
ALLOWED_SERVICES_META_KEY = "allowed_services"
LEGACY_MANIFEST_NETWORK_KEY = "network"


def _network_was_off(raw: str | None) -> bool:
    """The legacy gate's reading: an unreadable value read as on."""
    try:
        policy = json.loads(raw or "")
    except ValueError:
        return False
    if not isinstance(policy, dict):
        return False
    mode = policy.get("mode")
    if mode in ("on", "off"):
        return mode == "off"
    default = policy.get("organization_default", policy.get("org_default"))
    return default == "off"


def upgrade_network_policy(project: Any) -> None:
    with write_transaction(project.db, what="network setting upgrade"):
        rows = dict(
            project.db.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?)",
                (NETWORK_POLICY_META_KEY, ALLOWED_SERVICES_META_KEY),
            ).fetchall()
        )
        if NETWORK_POLICY_META_KEY not in rows:
            return
        allowed = rows.get(ALLOWED_SERVICES_META_KEY)
        if _network_was_off(rows[NETWORK_POLICY_META_KEY]) and allowed in (
            None,
            "all_services",
        ):
            folded = (
                "this_machine_only"
                if workspace_edition(project.path.parent) == "cloud"
                else "approved_services"
            )
            project.db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (ALLOWED_SERVICES_META_KEY, folded),
            )
        project.db.execute("DELETE FROM meta WHERE key=?", (NETWORK_POLICY_META_KEY,))
        stored = project.db.execute(
            "SELECT value FROM meta WHERE key=?", (ALLOWED_SERVICES_META_KEY,)
        ).fetchone()
    with manifest_lock(project.path):
        try:
            manifest = json.loads((project.path / MANIFEST_NAME).read_text())
        except (OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict):
            manifest.pop(LEGACY_MANIFEST_NETWORK_KEY, None)
            manifest[ALLOWED_SERVICES_META_KEY] = (
                stored[0] if stored is not None else "all_services"
            )
            write_manifest_atomically(project.path, manifest)
