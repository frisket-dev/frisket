"""HTTP contracts for the team-local model catalog and pull routes."""

from __future__ import annotations

from typing import Literal

from pydantic import JsonValue

from frisket.contracts.http.models import NamedCoerciveRequest, WireModel


class _CompatibleRequest(NamedCoerciveRequest):
    """Keep the pre-contract operational bodies' permissive wire behavior."""


class TeamArtifactPullRequest(_CompatibleRequest):
    ref: str
    unpinned_acknowledged: bool = False


class TeamModelPullError(WireModel):
    code: str | None
    message: str | None


class TeamModelPullArtifact(WireModel):
    kind: str
    source_url: str | None
    license: str | None
    manifest_version: str | None


class TeamModelPull(WireModel):
    schemaVersion: Literal["frisket.model_pull.v3"]
    id: int
    model: str
    status: Literal[
        "pending",
        "running",
        "done",
        "failed",
        "cancelled",
        "uninstalled",
    ]
    phase: str | None
    total_bytes: int | None
    completed_bytes: int | None
    error: TeamModelPullError | None
    resolved_digest: str | None
    resolved_size: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    cancel_requested: bool
    endpoint_id: str | None
    endpoint_origin: str | None
    initiated_by: str | None
    artifact: TeamModelPullArtifact | None


class TeamModelPullStartResponse(WireModel):
    pull: TeamModelPull
    deduplicated: bool


class TeamModelPullListResponse(WireModel):
    pulls: list[TeamModelPull]


class TeamModelPullDisabledDetail(WireModel):
    code: Literal["model_pull_disabled"]
    message: str


class TeamModelInvalidRefDetail(WireModel):
    code: Literal["invalid_model_ref"]
    message: str


class TeamModelUnpinnedUnacknowledgedDetail(WireModel):
    code: Literal["unpinned_unacknowledged"]
    message: str


class TeamModelLocalServerUnreachableDetail(WireModel):
    code: Literal["local_server_unreachable"]
    message: str
    url: str | None = None


class TeamModelLocalServerUnauthorizedDetail(WireModel):
    code: Literal["local_server_unauthorized"]
    message: str
    url: str


class TeamModelPullUnsupportedDetail(WireModel):
    code: Literal["pull_unsupported"]
    message: str
    url: str
    protocol: str


class TeamModelEnqueueFailedDetail(WireModel):
    code: Literal["enqueue_failed"]
    message: str


class TeamModelPullNotFoundDetail(WireModel):
    code: Literal["pull_not_found"]
    message: str


class TeamModelPullNotCancellableDetail(WireModel):
    code: Literal["pull_not_cancellable"]
    message: str


class TeamModelPullBusyDetail(WireModel):
    code: Literal["pull_busy"]
    message: str
    active: TeamModelPull | None = None


TeamModelCodedDetail = (
    TeamModelPullDisabledDetail
    | TeamModelInvalidRefDetail
    | TeamModelUnpinnedUnacknowledgedDetail
    | TeamModelLocalServerUnreachableDetail
    | TeamModelLocalServerUnauthorizedDetail
    | TeamModelPullUnsupportedDetail
    | TeamModelEnqueueFailedDetail
    | TeamModelPullNotFoundDetail
    | TeamModelPullNotCancellableDetail
    | TeamModelPullBusyDetail
)


class TeamModelHttpError(WireModel):
    """Closed envelope for every declared team-local-model HTTP error."""

    detail: str | list[JsonValue] | TeamModelCodedDetail


def team_model_http_error_responses(
    *statuses: int,
) -> dict[int, dict[str, type[TeamModelHttpError]]]:
    """Declare the team-local-model error envelope on an owning route."""

    return {status: {"model": TeamModelHttpError} for status in statuses}


__all__ = [
    "TeamArtifactPullRequest",
    "TeamModelHttpError",
    "TeamModelPull",
    "TeamModelPullArtifact",
    "TeamModelPullBusyDetail",
    "TeamModelPullError",
    "TeamModelPullListResponse",
    "TeamModelPullStartResponse",
    "team_model_http_error_responses",
]
