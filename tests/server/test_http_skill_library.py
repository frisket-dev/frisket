"""Runtime contract for text-only, revision-pinned instruction skills."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

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
    assert [item["id"] for item in client.get("/api/skills").json()["skills"]] == [uploaded.json()["id"]]


def test_skill_library_refuses_invalid_or_unsupported_packages_and_enforces_authority(tmp_path) -> None:
    client = _client(tmp_path)
    invalid = client.post("/api/skills", json={"content": "# No frontmatter"})
    assert invalid.status_code == 422
    assert "name" in invalid.json()["detail"]

    extra_frontmatter = client.post(
        "/api/skills",
        json={"content": SKILL.replace("description:", "allowed-tools: shell\ndescription:")},
    )
    assert extra_frontmatter.status_code == 422
    assert "unsupported" in extra_frontmatter.json()["detail"]

    wrong_upload = client.post(
        "/api/skills/upload",
        content=b"PK\x03\x04not a skill",
        headers={"content-type": "application/zip"},
    )
    assert wrong_upload.status_code == 415

    denied = _client(tmp_path / "denied", allowed=False)
    assert denied.get("/api/skills").status_code == 403
    assert denied.post("/api/skills", json={"content": SKILL}).status_code == 403
