"""The service access one project's work runs under.

Combines the project's Allowed services setting with the workspace's approved
destinations: the approved endpoints list and the origins of the configured
model servers, since adding a model server approves it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from frisket.local_provider_config import resolve_local_endpoints
from frisket.service_access import (
    DEFAULT_PORTS,
    ApprovedEndpoint,
    ServiceAccess,
    host_is_loopback,
    read_approved_endpoints,
    workspace_edition,
)

if TYPE_CHECKING:
    from frisket.engine.store.project import Project


def configured_model_server_endpoints(
    workspace_root: str | Path,
) -> tuple[ApprovedEndpoint, ...]:
    """Adding a model server approves its origin; removing it withdraws that.

    A loopback server is this machine already and needs no list entry.
    """
    endpoints, _notes = resolve_local_endpoints(workspace_root)
    servers: list[ApprovedEndpoint] = []
    for endpoint in endpoints:
        parsed = urlparse(endpoint.origin)
        host = parsed.hostname or ""
        if not host or host_is_loopback(host):
            continue
        servers.append(
            ApprovedEndpoint(
                host=host,
                port=parsed.port or DEFAULT_PORTS.get(parsed.scheme),
                label=endpoint.display_name[:120],
            )
        )
    return tuple(servers)


def project_service_access(project: Project) -> ServiceAccess:
    """The service access a project's work runs under.

    The project bundle sits in its workspace directory, which is also where
    the approved endpoints list and the model server settings live.
    """
    setting = project.allowed_services()
    if setting == "all_services":
        return ServiceAccess(setting)
    root = project.path.parent
    return ServiceAccess(
        setting,
        read_approved_endpoints(root) + configured_model_server_endpoints(root),
        frisket_processing_allowed=workspace_edition(root) == "cloud",
    )
