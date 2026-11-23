"""Service access policy: which services a project lets Frisket use.

A project's Allowed services setting is one of three values. Before Frisket
sends project data to a destination, the destination is classified as this
machine (loopback, a Unix socket, or in-process), an approved endpoint (a host,
and port if given, on the workspace's approved endpoints list), or other, and a
destination the setting does not allow is refused before the request is sent.

This is a best-effort check over Frisket's own code paths. It is not network
isolation: it does not stop a plugin, a subprocess or the operating system from
reaching the network. Plugin subprocesses and notification delivery are not
checked. API-call and research fetches are judged by their declared category,
not their host, so an approved host does not allow them; they need All
services.

Approved endpoints live at the workspace level, next to the model servers and
provider keys in ``<workspace>/.frisket/``. They are not secrets, so they keep
their own file: a malformed list must never make a provider-key write lossy.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlparse

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

AllowedServices = Literal["this_machine_only", "approved_services", "all_services"]
ALLOWED_SERVICES_VALUES: tuple[AllowedServices, ...] = get_args(AllowedServices)
DEFAULT_ALLOWED_SERVICES: AllowedServices = "all_services"
ALLOWED_SERVICES_LABELS: dict[AllowedServices, str] = {
    "this_machine_only": "This machine only",
    "approved_services": "Approved services",
    "all_services": "All services",
}
# Position in this tuple is strictness: a higher index allows more.
_LOOSENESS = {value: index for index, value in enumerate(ALLOWED_SERVICES_VALUES)}

DestinationClass = Literal["this_machine", "approved", "other"]

SERVICE_NOT_ALLOWED = "service_not_allowed"
MAX_APPROVED_ENDPOINTS = 64
DEFAULT_PORTS = {"http": 80, "https": 443}


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def host_is_loopback(host: str) -> bool:
    """Whether ``host`` names this machine's loopback interface."""
    return host.lower() in _LOOPBACK_HOSTS


def is_looser(current: AllowedServices, requested: AllowedServices) -> bool:
    """Whether ``requested`` allows more destinations than ``current``."""
    return _LOOSENESS[requested] > _LOOSENESS[current]


_WORKSPACE_EDITIONS: dict[str, str] = {}


def register_workspace_edition(workspace_root: str | Path, edition: str) -> None:
    """Record which edition serves the projects under ``workspace_root``.

    The execution paths that check a project's destinations hold the project,
    not its server, so the edition is looked up by the bundle's workspace.
    An unregistered workspace is a Local one.
    """
    _WORKSPACE_EDITIONS[str(Path(workspace_root).resolve())] = edition


def workspace_edition(workspace_root: str | Path) -> str:
    return _WORKSPACE_EDITIONS.get(str(Path(workspace_root).resolve()), "solo")


def allowed_services_offered(edition: str, value: AllowedServices) -> bool:
    """Frisket Cloud does not offer This machine only: projects live there."""
    return not (edition == "cloud" and value == "this_machine_only")


def setting_allows(setting: AllowedServices, destination: DestinationClass) -> bool:
    if destination == "this_machine":
        return True
    if destination == "approved":
        return setting != "this_machine_only"
    return setting == "all_services"


class ServiceNotAllowed(Exception):
    """A destination the project's Allowed services setting does not allow."""

    code = SERVICE_NOT_ALLOWED

    def __init__(self, setting: AllowedServices, destination: str) -> None:
        super().__init__(
            f"This project's Allowed services setting is "
            f"{ALLOWED_SERVICES_LABELS[setting]}; it does not allow {destination}. "
            "Change Allowed services in the project settings to use it."
        )
        self.setting = setting
        self.destination = destination


class ApprovedEndpoint(BaseModel):
    """One destination a person approved: a host, an optional port, a label."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    label: str = Field(default="", max_length=120)

    @field_validator("host")
    @classmethod
    def _bare_host(cls, value: str) -> str:
        host = value.strip().lower().strip("[]").rstrip(".")
        if not host or any(char in host for char in "/@?#\\ ") or "://" in value:
            raise ValueError("host must be a bare host name or address")
        return host

    @field_validator("label")
    @classmethod
    def _trim_label(cls, value: str) -> str:
        return value.strip()

    def matches(self, host: str, port: int | None) -> bool:
        return self.host == host and (self.port is None or self.port == port)


class ApprovedEndpoints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoints: tuple[ApprovedEndpoint, ...] = Field(
        default=(), max_length=MAX_APPROVED_ENDPOINTS
    )


class ApprovedEndpointList(ApprovedEndpoints):
    """The list as a reader sees it, with whether that reader may replace it."""

    editable: bool


class InvalidApprovedEndpointsError(ValueError):
    """The stored approved endpoints list cannot be read without data loss."""


def approved_endpoints_path(workspace_root: str | Path) -> Path:
    return Path(workspace_root) / ".frisket" / "approved_endpoints.json"


def read_approved_endpoints(
    workspace_root: str | Path,
) -> tuple[ApprovedEndpoint, ...]:
    """The workspace's approved endpoints; an absent file approves nothing."""
    path = approved_endpoints_path(workspace_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError) as exc:
        raise InvalidApprovedEndpointsError(
            f"cannot read approved endpoints at {path}"
        ) from exc
    try:
        return ApprovedEndpoints.model_validate_json(raw).endpoints
    except ValidationError as exc:
        raise InvalidApprovedEndpointsError(
            f"approved endpoints at {path} are not a valid list"
        ) from exc


def write_approved_endpoints(
    workspace_root: str | Path, endpoints: ApprovedEndpoints
) -> tuple[ApprovedEndpoint, ...]:
    """Replace the workspace's approved endpoints list as one atomic publish."""
    path = approved_endpoints_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitignore = path.parent / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text("*\n", encoding="utf-8")
    with FileLock(str(path.with_name(f".{path.name}.lock")), timeout=-1):
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(endpoints.model_dump(), indent=2))
                handle.write("\n")
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return endpoints.endpoints


def classify_destination(
    url: str, approved: tuple[ApprovedEndpoint, ...]
) -> DestinationClass:
    """Classify the destination a request is about to use.

    Only the actual host counts, never a provider's or server's name: a model
    server called "Ollama" on a remote host is not this machine, and approving
    a self-hosted OpenAI-compatible host does not approve api.openai.com.
    """
    parsed = urlparse(url)
    if parsed.scheme in {"unix", "http+unix"}:
        return "this_machine"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "other"
    if host_is_loopback(host):
        return "this_machine"
    try:
        port = parsed.port or DEFAULT_PORTS.get(parsed.scheme)
    except ValueError:
        return "other"
    if any(endpoint.matches(host, port) for endpoint in approved):
        return "approved"
    return "other"


@dataclass(frozen=True)
class ServiceAccess:
    """One project's setting with the workspace's approved endpoints.

    ``frisket_processing_allowed`` holds on Frisket Cloud, where the project
    already lives on Frisket's servers: Frisket's own processing there is
    allowed under every offered choice. Elsewhere a Frisket Cloud route is a
    remote service.
    """

    allowed_services: AllowedServices
    approved_endpoints: tuple[ApprovedEndpoint, ...] = ()
    frisket_processing_allowed: bool = False

    @property
    def allows_all_services(self) -> bool:
        return self.allowed_services == "all_services"

    def allows_url(self, url: str) -> bool:
        return setting_allows(
            self.allowed_services, classify_destination(url, self.approved_endpoints)
        )

    def check_url(self, url: str) -> None:
        """Raise ``ServiceNotAllowed`` unless the setting allows ``url``'s host."""
        if not self.allows_url(url):
            raise ServiceNotAllowed(
                self.allowed_services, urlparse(url).hostname or "this destination"
            )

    def check_remote_service(self, service: str) -> None:
        """Refuse a service with no known destination unless all are allowed.

        Provider APIs, Frisket Cloud routes, web search, page capture, media
        downloads, feeds and remote OCR or transcription reach hosts that are
        neither this machine nor an approved endpoint.
        """
        if not self.allows_all_services:
            raise ServiceNotAllowed(self.allowed_services, service)


__all__ = [
    "ALLOWED_SERVICES_LABELS",
    "ALLOWED_SERVICES_VALUES",
    "AllowedServices",
    "ApprovedEndpoint",
    "ApprovedEndpointList",
    "ApprovedEndpoints",
    "DEFAULT_ALLOWED_SERVICES",
    "DestinationClass",
    "InvalidApprovedEndpointsError",
    "SERVICE_NOT_ALLOWED",
    "ServiceAccess",
    "ServiceNotAllowed",
    "allowed_services_offered",
    "approved_endpoints_path",
    "DEFAULT_PORTS",
    "classify_destination",
    "host_is_loopback",
    "is_looser",
    "read_approved_endpoints",
    "register_workspace_edition",
    "setting_allows",
    "workspace_edition",
    "write_approved_endpoints",
]
