"""Typed, project-scoped settings.

Persisted as one validated JSON document under a single ``meta`` key rather
than a key per setting: the model is the schema, so a value that no longer
parses is a versioning problem to solve here, not a surprise at the read site.

Reads tolerate unknown persisted keys (a project written by a newer build stays
readable); writes reject them, so a typo fails loudly instead of being stored
and silently ignored forever.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROJECT_SETTINGS_META_KEY = "project_settings_v1"
PROJECT_MCP_SERVERS_META_KEY = "project_mcp_servers_v1"


class ProjectSettings(BaseModel):
    """Every project-scoped setting, with its default."""

    model_config = ConfigDict(extra="ignore")

    media_allow_private_hosts: bool | None = Field(
        default=None,
        description=(
            "Whether media downloads may reach loopback and RFC1918 "
            "addresses. None inherits the server default "
            "(FRISKET_MEDIA_PRIVATE_HOSTS; unset means allow — a media host "
            "on the operator's own network is a legitimate target). The "
            "server can lock this off, in which case the stored value is "
            "ignored. Link-local and instance-metadata addresses are refused "
            "regardless of this setting. Effective resolution lives in "
            "frisket.ops.egress_policy.media_egress_policy."
        ),
    )


class ProjectSettingsPatch(BaseModel):
    """A partial update. Unknown keys are an error, not a no-op."""

    model_config = ConfigDict(extra="forbid")

    media_allow_private_hosts: bool | None = None


def read_project_settings(project: Any) -> ProjectSettings:
    """Settings for ``project``, falling back to defaults.

    Never raises: an absent, unparseable, or non-object document yields
    defaults, because a corrupt settings blob must not make a project
    unopenable.
    """
    raw = project.get_meta(PROJECT_SETTINGS_META_KEY)
    if not raw:
        return ProjectSettings()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ProjectSettings()
    if not isinstance(parsed, dict):
        return ProjectSettings()
    try:
        return ProjectSettings.model_validate(parsed)
    except ValidationError:
        return ProjectSettings()


def write_project_settings(project: Any, settings: ProjectSettings) -> ProjectSettings:
    """Persist ``settings`` whole and return what was stored."""
    project.set_meta(PROJECT_SETTINGS_META_KEY, settings.model_dump_json())
    return settings


def patch_project_settings(project: Any, patch: Any) -> ProjectSettings:
    """Merge a partial update over the stored settings and persist the result.

    ``patch`` is any mapping; unknown keys raise ``ValidationError`` so a caller
    learns its field name was wrong instead of watching the value vanish.
    """
    validated = ProjectSettingsPatch.model_validate(patch)
    updates = validated.model_dump(exclude_none=True)
    merged = read_project_settings(project).model_copy(update=updates)
    return write_project_settings(project, merged)


def read_project_mcp_servers(project: Any) -> list[dict[str, Any]]:
    """Return the independently-versioned, project-local MCP server records.

    MCP executable configuration is intentionally not folded into the general
    project-settings document.  It has its own lifecycle, and a malformed old
    record must not prevent the project (or ordinary settings) from opening.
    The route service is the sole writer and validates every record before it
    reaches this small persistence boundary.
    """
    raw = project.get_meta(PROJECT_MCP_SERVERS_META_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list) or not all(
        isinstance(server, dict) for server in parsed
    ):
        return []
    return parsed


def write_project_mcp_servers(
    project: Any, servers: list[dict[str, Any]], *, commit: bool = True
) -> None:
    """Persist already-validated MCP server configuration.

    Callers replacing secret-consumer references pass ``commit=False`` so the
    metadata and join-table changes share the same project SQLite transaction.
    """
    project.set_meta(
        PROJECT_MCP_SERVERS_META_KEY, json.dumps(servers, sort_keys=True), commit=commit
    )
