"""The service access one project's work runs under.

Combines the project's Allowed services setting with the workspace's approved
destinations: the approved endpoints list and the origins of the configured
model servers, since adding a model server approves it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from frisket.ai.llm.types import provider_from_model_id
from frisket.ai.models.metadata import is_remote_provider
from frisket.execution.definitions import MODELS_GATEWAY_TARGET_ID
from frisket.local_provider_config import (
    resolve_local_endpoints,
    resolve_models_gateway,
)
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


FRISKET_ROUTE_EGRESS_CLASSES = frozenset({"frisket_shared", "frisket_dedicated_org"})


@dataclass(frozen=True)
class SelectedRoute:
    """The execution target a run was resolved to."""

    target_id: str
    egress_class: str


def selected_target_destination(project: Project, target_id: str) -> str | None:
    """The origin a target on the operator's own network sends to, if known.

    The models gateway's origin is the one the operator configured, from the
    environment or the workspace settings.
    """
    if target_id != MODELS_GATEWAY_TARGET_ID:
        return None
    gateway = resolve_models_gateway(project.path.parent)
    return None if gateway is None else gateway.origin


def check_route(
    project: Project,
    access: ServiceAccess,
    route: SelectedRoute | None,
    *,
    model_destination: str | None = None,
) -> None:
    """Raise ``ServiceNotAllowed`` unless ``access`` allows where the work goes.

    The one decision execution and the selectors share. Frisket Cloud routes
    count as Frisket's own processing only on Frisket Cloud. Otherwise a model
    call is judged by the URL the router sends it to, a target on the
    operator's own network by its configured origin, and anything else needs
    All services.
    """
    if access.allows_all_services:
        return
    egress_class = route.egress_class if route is not None else None
    if egress_class in FRISKET_ROUTE_EGRESS_CLASSES:
        if not access.frisket_processing_allowed:
            access.check_remote_service("Frisket Cloud")
        return
    if model_destination is not None:
        access.check_url(model_destination)
        return
    if route is None or egress_class == "none":
        return
    if egress_class == "operator_lan":
        destination = selected_target_destination(project, route.target_id)
        if destination is not None:
            access.check_url(destination)
            return
    access.check_remote_service(route.target_id)


def check_model_call(
    project: Project,
    model: str,
    *,
    destination: str | None,
    route: SelectedRoute | None = None,
) -> None:
    """Raise ``ServiceNotAllowed`` unless the project may send this model call.

    ``destination`` is the URL the router sends ``model`` to. A remote
    provider whose URL is not known needs All services, unless the call goes
    through a Frisket Cloud route, which ``check_route`` judges.
    """
    access = project_service_access(project)
    if (
        destination is None
        and (route is None or route.egress_class not in FRISKET_ROUTE_EGRESS_CLASSES)
        and is_remote_provider(provider_from_model_id(model))
    ):
        access.check_remote_service(provider_from_model_id(model))
    check_route(project, access, route, model_destination=destination)
