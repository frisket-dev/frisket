"""Convert the stored ``sensitive`` flag into the usage-data sharing override.

A project marked sensitive shares no usage data ("off"); every other project
follows the workspace ("inherit"). The legacy key is removed in the same
transaction so the override is the project's only stored value. A project
whose meta lost the flag while its manifest still records it keeps sharing
nothing: either store marking it sensitive is enough. The manifest is then
rewritten from the value actually stored, never from a recomputed one.
"""

from __future__ import annotations

import json
from typing import Any

from frisket.engine.store.transactions import write_transaction

LEGACY_SENSITIVE_META_KEY = "sensitive"
USAGE_DATA_SHARING_META_KEY = "usage_data_sharing"
MANIFEST_NAME = "manifest.json"


def _manifest_shares_nothing(project: Any) -> bool:
    try:
        manifest = json.loads((project.path / MANIFEST_NAME).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(manifest, dict) and (
        manifest.get(LEGACY_SENSITIVE_META_KEY) is True
        or manifest.get(USAGE_DATA_SHARING_META_KEY) == "off"
    )


def upgrade_usage_data_sharing(project: Any) -> None:
    manifest_off = _manifest_shares_nothing(project)
    with write_transaction(project.db, what="usage data sharing upgrade"):
        row = project.db.execute(
            "SELECT value FROM meta WHERE key=?", (LEGACY_SENSITIVE_META_KEY,)
        ).fetchone()
        sensitive = manifest_off or (row is not None and row[0] == "true")
        project.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (USAGE_DATA_SHARING_META_KEY, "off" if sensitive else "inherit"),
        )
        project.db.execute("DELETE FROM meta WHERE key=?", (LEGACY_SENSITIVE_META_KEY,))
        stored = project.db.execute(
            "SELECT value FROM meta WHERE key=?", (USAGE_DATA_SHARING_META_KEY,)
        ).fetchone()[0]
    project._write_manifest(usage_data_sharing=stored, sensitive=stored == "off")
