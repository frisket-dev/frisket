"""Strict HTTP contracts for project-member and project-invite routes."""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field, RootModel

from frisket.contracts.http.models import WireModel


class SetProjectMemberRequest(WireModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["viewer", "reviewer", "editor", "owner"]


class CreateProjectInviteRequest(WireModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["viewer", "editor"] = "viewer"


class ProjectMember(WireModel):
    user_id: int = Field(gt=0)
    email: str
    role: Literal["viewer", "reviewer", "editor", "owner"]


class ProjectMemberList(RootModel[list[ProjectMember]]):
    model_config = ConfigDict(strict=True)


class ProjectMemberSet(WireModel):
    ok: bool
    email: str
    role: Literal["viewer", "reviewer", "editor", "owner"]


class ProjectMemberRemove(WireModel):
    ok: bool
    removed: bool


class ProjectInvite(WireModel):
    id: int = Field(gt=0)
    project_id: str = Field(min_length=1)
    email: str
    role: Literal["viewer", "editor"]
    created_at: str
    expires_at: str
    accepted_at: str | None
    revoked_at: str | None


class ProjectInviteList(WireModel):
    invites: list[ProjectInvite]


class ProjectInviteCreate(WireModel):
    sent: bool
    invite: ProjectInvite


class ProjectInviteRevoke(WireModel):
    ok: bool
    revoked: bool


__all__ = [
    "CreateProjectInviteRequest",
    "ProjectInvite",
    "ProjectInviteCreate",
    "ProjectInviteList",
    "ProjectInviteRevoke",
    "ProjectMember",
    "ProjectMemberList",
    "ProjectMemberRemove",
    "ProjectMemberSet",
    "SetProjectMemberRequest",
]
