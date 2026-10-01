"""Runtime contract for text-only, revision-pinned instruction skills."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
import pytest

from frisket.contracts.http.endpoint_catalog import base_endpoint_groups
from frisket.server.routes.skills import register_skill_routes
from frisket.server.services.skills import SkillLibrary


SKILL = """---
name: investigate-documents
description: Check representative records, alternatives, coverage, and citations.
---

# Investigate documents

Use existing actions and cite the outputs you inspect.
"""


def _client(tmp_path, *, allowed: bool = True) -> TestClient:
    app = FastAPI()

    def require_manager(_request: Request) -> None:
        if not allowed:
            raise HTTPException(403, "organization administrator required")

    register_skill_routes(
        app,
        library=SkillLibrary(tmp_path / "workspace"),
        require_manager=require_manager,
    )
    return TestClient(app)


def test_skill_editor_upload_and_activation_keep_an_admitted_revision(tmp_path) -> None:
    client = _client(tmp_path)

    created = client.post("/api/skills", json={"content": SKILL, "enabled": True})
    assert created.status_code == 200, created.text
    skill = created.json()
    assert skill["name"] == "investigate-documents"
    assert skill["description"].startswith("Check representative")
    assert skill["revision"] == 1
    assert skill["enabled"] is True
    assert skill["content"] == SKILL

    listed = client.get("/api/skills")
    assert listed.status_code == 200
    assert listed.json()["skills"] == [skill]

    uploaded = client.post(
        "/api/skills/upload",
        content=SKILL.replace("investigate-documents", "source-reading", 1).encode(),
        headers={"content-type": "text/markdown; charset=utf-8"},
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["name"] == "source-reading"

    changed = client.patch(
        f"/api/skills/{skill['id']}/enabled",
        json={"expectedRevision": 1, "enabled": False},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["enabled"] is False
    assert changed.json()["revision"] == 2

    updated_content = SKILL.replace("Use existing actions", "Inspect existing actions")
    updated = client.put(
        f"/api/skills/{skill['id']}",
        json={"expectedRevision": 2, "content": updated_content},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 3
    assert updated.json()["content"] == updated_content

    stale = client.put(
        f"/api/skills/{skill['id']}",
        json={"expectedRevision": 2, "content": SKILL},
    )
    assert stale.status_code == 409

    deleted = client.request(
        "DELETE", f"/api/skills/{skill['id']}", json={"expectedRevision": 3}
    )
    assert deleted.status_code == 204
    assert [item["id"] for item in client.get("/api/skills").json()["skills"]] == [
        uploaded.json()["id"]
    ]


def test_skill_library_refuses_invalid_or_unsupported_packages_and_enforces_authority(
    tmp_path,
) -> None:
    client = _client(tmp_path)
    invalid = client.post("/api/skills", json={"content": "# No frontmatter"})
    assert invalid.status_code == 422
    assert "name" in invalid.json()["detail"]

    extra_frontmatter = client.post(
        "/api/skills",
        json={
            "content": SKILL.replace(
                "description:", "allowed-tools: shell\ndescription:"
            )
        },
    )
    assert extra_frontmatter.status_code == 422
    assert "unsupported" in extra_frontmatter.json()["detail"]

    mixed_key = client.post(
        "/api/skills",
        json={"content": SKILL.replace("description:", "1: unsupported\ndescription:")},
    )
    assert mixed_key.status_code == 422
    assert "unsupported fields: 1" in mixed_key.json()["detail"]

    wrong_upload = client.post(
        "/api/skills/upload",
        content=b"PK\x03\x04not a skill",
        headers={"content-type": "application/zip"},
    )
    assert wrong_upload.status_code == 415

    malformed_upload = client.post(
        "/api/skills/upload",
        content=b"\xff\xfe",
        headers={"content-type": "text/markdown"},
    )
    assert malformed_upload.status_code == 422
    assert "UTF-8" in malformed_upload.json()["detail"]

    denied = _client(tmp_path / "denied", allowed=False)
    assert denied.get("/api/skills").status_code == 403
    assert denied.post("/api/skills", json={"content": SKILL}).status_code == 403


def test_team_library_factory_isolated_by_authorized_request_tenant(tmp_path) -> None:
    app = FastAPI()
    resolved: list[str] = []

    def require_manager(request: Request) -> None:
        if request.headers.get("x-org") not in {"alpha", "beta"}:
            raise HTTPException(403, "organization administrator required")

    def library_for(request: Request) -> SkillLibrary:
        org = request.headers["x-org"]
        resolved.append(org)
        return SkillLibrary(tmp_path / "organizations" / org)

    register_skill_routes(app, library=library_for, require_manager=require_manager)
    client = TestClient(app)

    alpha = client.post(
        "/api/skills", headers={"x-org": "alpha"}, json={"content": SKILL}
    )
    beta = client.post(
        "/api/skills",
        headers={"x-org": "beta"},
        json={"content": SKILL.replace("investigate-documents", "source-reading", 1)},
    )
    assert alpha.status_code == 200 and beta.status_code == 200
    assert [
        item["name"]
        for item in client.get("/api/skills", headers={"x-org": "alpha"}).json()[
            "skills"
        ]
    ] == ["investigate-documents"]
    assert [
        item["name"]
        for item in client.get("/api/skills", headers={"x-org": "beta"}).json()[
            "skills"
        ]
    ] == ["source-reading"]

    before_denial = list(resolved)
    assert client.get("/api/skills", headers={"x-org": "untrusted"}).status_code == 403
    assert resolved == before_denial


def test_team_endpoint_policy_classifies_each_skill_management_route() -> None:
    ids = {endpoint.id for endpoint in base_endpoint_groups()["tenant.admin"]}
    assert {
        "tenant.list_skills.get",
        "tenant.create_skill.post",
        "tenant.upload_skill.post",
        "tenant.update_skill.put",
        "tenant.set_skill_enabled.patch",
        "tenant.delete_skill.delete",
    } <= ids


def test_solo_app_registers_the_workspace_skill_library(tmp_path) -> None:
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    from frisket.server.app import create_app

    with TestClient(create_app(tmp_path / "workspace")) as client:
        response = client.get("/api/skills")
    assert response.status_code == 200
    assert response.json() == {"schemaVersion": "frisket.skills.v1", "skills": []}


@pytest.mark.parametrize("name", ["-leading", "trailing-", "double--hyphen", "a" * 65])
def test_skill_name_matches_harness_slug_constraints(tmp_path, name: str) -> None:
    client = _client(tmp_path)
    rejected = client.post(
        "/api/skills",
        json={"content": SKILL.replace("investigate-documents", name, 1)},
    )
    assert rejected.status_code == 422
    assert "single hyphens" in rejected.json()["detail"]
