from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_lifecycle_routes_preserve_http_contract(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))

    created = client.post("/api/projects", json={"name": "Project Routes"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    # Rebaselined 2026-07-02: the settings revamp changed workspace.create() and
    # workspace.list() to include description and sensitive in the project payload.
    assert created.json() == {
        "id": "project-routes",
        "name": "Project Routes",
        "description": "",
        "sensitive": False,
    }

    listed = client.get("/api/projects")
    assert listed.status_code == 200
    listed_body = listed.json()
    assert len(listed_body) == 1
    assert listed_body[0]["id"] == pid
    assert listed_body[0]["name"] == "Project Routes"
    assert listed_body[0]["description"] == ""
    assert listed_body[0]["sensitive"] is False
    # onboard2-picker-day2-badges-v1: list rows also carry a last-modified
    # (bundle project.db mtime) and a cached pending-review count so
    # ProjectPicker can render both without opening every project's bundle.
    assert listed_body[0]["updated_at"]
    assert listed_body[0]["pending_review_count"] == 0

    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("Facts")
    column_id = project.add_column(sheet_id, "Title", type="text")
    sheets = client.get(f"/api/projects/{pid}/sheets")
    assert sheets.status_code == 200, sheets.text
    # Workbench IA inc 7: the sheets payload gained parent_op_id (op kind/label +
    # syncState/stale_reason are added only for DERIVED sheets, so a root sheet
    # carries just parent_op_id: None). sheet-title-column-v1: every entry also
    # carries title_column_id (None until the '...' menu's "Use as row title"
    # sets it — PATCH /api/projects/{pid}/sheets/{sheet_id}). Every entry also
    # carries cited_column_ids (the columns
    # with at least one cited-answer evidence link; empty until an answer cites
    # one — see frisket.store.evidence.sheet_cited_column_ids).
    # annotated_text_column_ids contains the SOURCE text columns reachable
    # through an annotation surface — the narrow signal that gates the
    # Document view's text reader, distinct from cited_column_ids.
    assert sheets.json() == [
        {
            "id": sheet_id,
            "name": "Facts",
            "parent_sheet_id": None,
            "parent_op_id": None,
            "rows": 0,
            "title_column_id": None,
            "cited_column_ids": [],
            "annotated_text_column_ids": [],
            "dependent_sheet_ids": [],
            "columns": [
                {
                    "id": column_id,
                    "name": "Title",
                    "type": "text",
                    "ai_generated": False,
                    "format": None,
                    "semantic_type": None,
                    "current_run_id": None,
                    "latest_run_id": None,
                    "generation_managed": False,
                    "mixed_origins": False,
                    "transcript_status": None,
                    "media_download_candidate": None,
                    "replay_pending_count": 0,
                    "default_hidden": False,
                }
            ],
        }
    ]

    scratch_id = project.add_sheet("Scratch")
    deleted_sheet = client.delete(f"/api/projects/{pid}/sheets/{scratch_id}")
    assert deleted_sheet.status_code == 200, deleted_sheet.text
    assert deleted_sheet.json() == {
        "ok": True,
        "deleted_sheet_id": scratch_id,
        "deleted_sheet_name": "Scratch",
    }
    assert [
        sheet["name"] for sheet in client.get(f"/api/projects/{pid}/sheets").json()
    ] == ["Facts"]

    derived_id = project.add_sheet("Derived", parent_sheet_id=sheet_id)
    blocked_sheet = client.delete(f"/api/projects/{pid}/sheets/{sheet_id}")
    assert blocked_sheet.status_code == 409, blocked_sheet.text
    assert "used by Derived" in blocked_sheet.json()["detail"]
    dependencies = client.get(f"/api/projects/{pid}/sheets").json()
    facts = next(sheet for sheet in dependencies if sheet["id"] == sheet_id)
    assert facts["dependent_sheet_ids"] == [derived_id]

    retention = client.get(f"/api/projects/{pid}/retention")
    assert retention.status_code == 200, retention.text
    assert retention.json()["default_evidence"] == "compactable"

    updated = client.patch(
        f"/api/projects/{pid}/retention",
        json={"default_evidence": "pinned", "no_compact": True},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["default_evidence"] == "pinned"
    assert updated.json()["no_compact"] is True

    invalid = client.patch(
        f"/api/projects/{pid}/retention",
        json={"default_evidence": "discard"},
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"] == "unsupported default_evidence: discard"

    missing = client.get("/api/projects/not-found/retention")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "no project 'not-found'"

    # Deletion is a danger-zone action: the project's name must be typed back
    # in the body and verified server-side. A missing body is a 422, a wrong
    # name is a 422 with the danger-zone message, and neither removes anything.
    missing_body = client.request("DELETE", f"/api/projects/{pid}")
    assert missing_body.status_code == 422, missing_body.text

    wrong_name = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "not the name"}
    )
    assert wrong_name.status_code == 422, wrong_name.text
    assert "type the project name" in wrong_name.json()["detail"]
    assert len(client.get("/api/projects").json()) == 1

    invalid_lifetime = client.request(
        "DELETE",
        f"/api/projects/{pid}",
        json={"confirm_name": "Project Routes", "project_row_id": 0},
    )
    assert invalid_lifetime.status_code == 422, invalid_lifetime.text
    assert len(client.get("/api/projects").json()) == 1

    deleted = client.request(
        "DELETE",
        f"/api/projects/{pid}",
        json={"confirm_name": "Project Routes", "project_row_id": 1},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"ok": True, "deleted": pid}
    assert client.get("/api/projects").json() == []
    # The bundle directory (blobs, op log, sidecars) is gone from disk, not just
    # delisted.
    assert not (tmp_path / "workspace" / f"{pid}.frisket").exists()


def test_project_delete_refused_while_runs_in_flight(tmp_path) -> None:
    """A correct name confirmation is still refused (409) while the project has
    a running job, so a deletion never races a live run. Once the job reaches a
    terminal state the same request succeeds."""
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Busy Project"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    workspace = client.app.state.workspace
    job_id = workspace.queue.enqueue("project.run", {"project_id": pid})
    claimed = workspace.queue.claim("project-delete-test-worker")
    assert claimed is not None
    assert claimed.id == job_id

    blocked = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Busy Project"}
    )
    assert blocked.status_code == 409, blocked.text
    assert "runs in flight" in blocked.json()["detail"]
    # Nothing was removed.
    assert (tmp_path / "workspace" / f"{pid}.frisket").exists()
    assert len(client.get("/api/projects").json()) == 1

    # Drain the job to a terminal state; deletion is then allowed.
    assert workspace.queue.complete(job_id, "project-delete-test-worker")
    deleted = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Busy Project"}
    )
    assert deleted.status_code == 200, deleted.text
    assert client.get("/api/projects").json() == []


def test_project_delete_refused_while_cancelled_handler_still_has_authority(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Handler Project"})
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    queue = client.app.state.workspace.queue
    job_id = queue.enqueue("action.run", {"project_id": pid})
    claimed = queue.claim("active-handler")
    assert claimed is not None and claimed.handler_authority_id
    assert queue.cancel(job_id)
    assert queue.get(job_id).status == "cancelled"

    blocked = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Handler Project"}
    )
    assert blocked.status_code == 409, blocked.text
    assert (tmp_path / "workspace" / f"{pid}.frisket").exists()

    assert queue.acknowledge_handler_exit(
        job_id,
        "active-handler",
        authority_id=claimed.handler_authority_id,
    )
    deleted = client.request(
        "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Handler Project"}
    )
    assert deleted.status_code == 200, deleted.text


def test_project_create_reserved_windows_device_name_gets_disambiguated(
    tmp_path,
) -> None:
    """A project named after a Windows reserved device stem (CON, PRN, AUX,
    NUL, COM1-9, LPT1-9) must not become that literal slug -- "con.frisket"
    still collides with the CON device on native Windows. The auto-slugifier
    disambiguates with a "-project" suffix before the collision-number loop
    by the reserved-name contract. A normal, non-reserved name is unaffected."""
    client = TestClient(create_app(tmp_path / "workspace"))

    normal = client.post("/api/projects", json={"name": "Normal Project"})
    assert normal.status_code == 200, normal.text
    assert normal.json()["id"] == "normal-project"

    lower = client.post("/api/projects", json={"name": "con"})
    assert lower.status_code == 200, lower.text
    assert lower.json()["id"] == "con-project"

    # A second reserved-name creation collides with "con-project" and falls
    # through the existing numbered-collision loop, same as any other name.
    upper = client.post("/api/projects", json={"name": "CON"})
    assert upper.status_code == 200, upper.text
    assert upper.json()["id"] == "con-project-2"

    mixed = client.post("/api/projects", json={"name": "Lpt1"})
    assert mixed.status_code == 200, mixed.text
    assert mixed.json()["id"] == "lpt1-project"


def test_project_lookup_and_delete_404_for_reserved_device_name_pid(
    tmp_path,
) -> None:
    """A reserved-device-stem pid in the URL (which never names a real
    project, since Workspace.create never produces one) is treated as
    "not found", not a 500 -- ProjectLifecycleService._manifest_exists
    treats a validate_project_slug failure as "not found" so these HTTP
    bindings keep returning 404 for an invalid route ID."""
    client = TestClient(create_app(tmp_path / "workspace"))
    for pid in ("con", "CON", "Con", "nul", "com1", "LPT9"):
        got = client.get(f"/api/projects/{pid}")
        assert got.status_code == 404, (pid, got.text)
        deleted = client.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": "anything"}
        )
        assert deleted.status_code == 404, (pid, deleted.text)
        sheets = client.get(f"/api/projects/{pid}/sheets")
        assert sheets.status_code == 404, (pid, sheets.text)


def test_non_lifecycle_routes_404_for_malformed_pid_instead_of_500(
    tmp_path,
) -> None:
    """Malformed project IDs (reserved device stem, disallowed character,
    over the 96-char length limit) must 404 through ANY route that calls
    Workspace.get() directly -- not only the lifecycle routes gated by
    ProjectLifecycleService._manifest_exists. Before this fix,
    Workspace.get() let validate_project_slug's bare ValueError propagate
    uncaught on routes that don't pre-check via the lifecycle service:
    GET /api/projects/con/sources and .../actions/v1/catalog reproduced as
    500. ``Workspace.get()`` now raises ``RouteError``
    (a ValueError subclass) instead, so the app-level RouteError handler
    (registered once, app-wide, in create_app -- not a blanket ValueError
    handler) converts it to 404 everywhere."""
    client = TestClient(create_app(tmp_path / "workspace"))
    malformed_pids = (
        "con",  # reserved Windows device stem
        "has space",  # disallowed character
        "a" * 97,  # over the 1-96 char length limit
    )
    for pid in malformed_pids:
        for path in (
            f"/api/projects/{pid}",
            f"/api/projects/{pid}/sources",
            f"/api/projects/{pid}/actions/v1/catalog",
            f"/api/projects/{pid}/sheets",
        ):
            got = client.get(path)
            assert got.status_code == 404, (pid, path, got.status_code, got.text)


def test_workspace_get_still_raises_valueerror_for_direct_python_callers(
    tmp_path,
) -> None:
    """A direct (non-HTTP) Workspace.get() call still raises ValueError for
    an invalid project_id. RouteError subclasses ValueError, so the
    direct-call contract still holds: Python callers receive ``ValueError``
    for invalid input even though the
    HTTP path now converts the identical failure into a 404 response."""
    from frisket.server.workspace import Workspace

    ws = Workspace(tmp_path / "workspace")
    with pytest.raises(ValueError):
        ws.get("con")
