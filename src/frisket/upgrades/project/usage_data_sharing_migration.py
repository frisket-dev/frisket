"""Convert the stored ``sensitive`` flag into the usage-data sharing override.

A project marked sensitive shares no usage data ("off"); every other project
follows the workspace ("inherit"). The legacy key is removed in the same
transaction so the override is the project's only stored value.
"""

from __future__ import annotations

from typing import Any

from frisket.engine.store.transactions import write_transaction

LEGACY_SENSITIVE_META_KEY = "sensitive"
USAGE_DATA_SHARING_META_KEY = "usage_data_sharing"


def upgrade_usage_data_sharing(project: Any) -> None:
    with write_transaction(project.db, what="usage data sharing upgrade"):
        row = project.db.execute(
            "SELECT value FROM meta WHERE key=?", (LEGACY_SENSITIVE_META_KEY,)
        ).fetchone()
        value = "off" if row is not None and row[0] == "true" else "inherit"
        project.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            (USAGE_DATA_SHARING_META_KEY, value),
        )
        project.db.execute("DELETE FROM meta WHERE key=?", (LEGACY_SENSITIVE_META_KEY,))
    project._write_manifest(usage_data_sharing=value, sensitive=value == "off")
