from __future__ import annotations

from copy import deepcopy
from typing import Any


MARKETPLACE_QUERY_SCHEMA_VERSION = "frisket.plugin_marketplace_query.v1"
MARKETPLACE_ENTRY_SCHEMA_VERSION = "frisket.plugin_index_entry.v1"
INSTALL_ATTEMPT_SCHEMA_VERSION = "frisket.plugin_install_attempt.v1"

_ALLOWED_ACTIONS = ["view_index_entry", "request_review"]
_DISABLED_ACTIONS = ["install", "enable", "trust", "load_package"]

_MARKETPLACE_ENTRIES: list[dict[str, Any]] = [
    {
        "schemaVersion": MARKETPLACE_ENTRY_SCHEMA_VERSION,
        "pluginId": "community.geo-map",
        "title": "Community geo map",
        "version": "0.8.1",
        "source": {"kind": "marketplace", "value": "community.geo-map"},
        "trustTier": "isolated",
        "compatibility": "compatible",
        "disabledReason": "marketplace_review_required",
        "contributionSummary": [
            {"kind": "view", "count": 1},
            {"kind": "command", "count": 1},
        ],
        "reviewStatus": "requires_review",
        "discoverableFor": ["frisket.geo.view.map"],
        "installActionsDisabled": True,
    },
    {
        "schemaVersion": MARKETPLACE_ENTRY_SCHEMA_VERSION,
        "pluginId": "community.bottom-dock-network",
        "title": "Network dock",
        "version": "2.0.0",
        "source": {"kind": "marketplace", "value": "community.bottom-dock-network"},
        "trustTier": "declarativeOnly",
        "compatibility": "incompatible",
        "disabledReason": "version_incompatible",
        "contributionSummary": [{"kind": "panel", "count": 1}],
        "reviewStatus": "blocked_by_version",
        "discoverableFor": ["frisket.geo.view.map"],
        "installActionsDisabled": True,
    },
]

_INSTALL_FAILURES: dict[str, dict[str, Any]] = {
    "community.graph": {
        "code": "checksum_mismatch",
        "message": "Downloaded plugin checksum did not match index entry.",
        "retryable": False,
    }
}


def workbench_marketplace_policy(
    *,
    project_id: str,
    contribution_id: str,
    text: str,
) -> dict[str, Any]:
    normalized = text.strip().lower()
    entries = [
        deepcopy(entry)
        for entry in _MARKETPLACE_ENTRIES
        if _matches_entry(entry, contribution_id=contribution_id, text=normalized)
    ]
    return {
        "schemaVersion": MARKETPLACE_QUERY_SCHEMA_VERSION,
        "projectId": project_id,
        "contributionId": contribution_id,
        "text": normalized,
        "policySource": "backend",
        "allowedActions": list(_ALLOWED_ACTIONS),
        "disabledActions": list(_DISABLED_ACTIONS),
        "arbitraryPackageLoadAllowed": False,
        "resultCount": len(entries),
        "entries": entries,
    }


def workbench_marketplace_install_attempt(
    *,
    project_id: str,
    plugin_id: str,
    version: str | None,
    source: dict[str, Any],
    arbitrary_package_load_allowed: bool,
) -> tuple[int, dict[str, Any]]:
    failure = _INSTALL_FAILURES.get(
        plugin_id,
        {
            "code": "marketplace_review_required",
            "message": "Marketplace plugins require review before install.",
            "retryable": False,
        },
    )
    status_code = 409
    if arbitrary_package_load_allowed:
        failure = {
            "code": "arbitrary_package_load_forbidden",
            "message": "Marketplace install attempts cannot enable arbitrary package loading.",
            "retryable": False,
        }
        status_code = 403
    return status_code, {
        "schemaVersion": INSTALL_ATTEMPT_SCHEMA_VERSION,
        "projectId": project_id,
        "pluginId": plugin_id,
        "version": version,
        "source": deepcopy(source),
        "installState": "failed",
        "failure": failure,
        "layoutMutated": False,
        "failureVisible": True,
        "arbitraryPackageLoadAllowed": False,
    }


def _matches_entry(entry: dict[str, Any], *, contribution_id: str, text: str) -> bool:
    if text == "":
        return True
    searchable = " ".join(
        [
            str(entry["pluginId"]).lower(),
            str(entry["title"]).lower(),
            str(entry["source"]["value"]).lower(),
        ]
    )
    if text in searchable:
        return True
    return text == "geo" and contribution_id in entry.get("discoverableFor", [])
