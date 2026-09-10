"""Canonical collaboration IDs and strict touched requests."""

import pytest
from pydantic import ValidationError

from frisket.contracts.http.project_collaboration import (
    CreateProjectInviteRequest,
    ProjectInvite,
    ProjectMember,
    SetProjectMemberRequest,
)


def test_collaboration_responses_have_one_semantic_identifier() -> None:
    member = ProjectMember.model_validate(
        {"user_id": 7, "email": "member@example.com", "role": "editor"}
    )
    invite = ProjectInvite.model_validate(
        {
            "id": 11,
            "project_id": "reporting",
            "email": "viewer@example.com",
            "role": "viewer",
            "created_at": "2026-08-29T00:00:00Z",
            "expires_at": "2026-09-12T00:00:00Z",
            "accepted_at": None,
            "revoked_at": None,
        }
    )
    assert member.model_dump() == {
        "user_id": 7,
        "email": "member@example.com",
        "role": "editor",
    }
    assert "slug" not in invite.model_dump()

    with pytest.raises(ValidationError):
        ProjectMember.model_validate({**member.model_dump(), "id": 7})
    with pytest.raises(ValidationError):
        ProjectInvite.model_validate({**invite.model_dump(), "slug": "reporting"})


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            SetProjectMemberRequest,
            {"email": "member@example.com", "role": "editor", "legacy": True},
        ),
        (
            CreateProjectInviteRequest,
            {"email": "viewer@example.com", "role": 1},
        ),
    ],
)
def test_touched_collaboration_requests_refuse_extra_or_coerced_values(
    model: type[SetProjectMemberRequest | CreateProjectInviteRequest],
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(payload)
