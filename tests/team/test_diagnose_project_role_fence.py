"""A project named to ``/api/diagnose`` must go through the project-role ladder.

``GET /api/diagnose`` took its project as a QUERY parameter while the RBAC
ladder resolves a project only from ``path_params["pid"]``
(``team/enforcement.py``). The catalog therefore classified it under
``tenant.session`` with ``project_role=None`` and the ladder never ran for the
project the handler then opened -- an org member with a role on project A could
read project B's plugin-runtime health, and a bad slug degraded to
``available:false``, making the route a project-existence oracle. The probe
also calls ``bootstrap_project_bundled_plugins`` on that bundle; it is
idempotent and normally already ran at project creation, so this is a latent
write rather than an observed one -- it lands the first time a bundled package
is new to an existing project, e.g. after an upgrade ships one.

The fix closes the shape rather than the instance: the project-scoped probe
moved onto ``/api/projects/{pid}/diagnose``, inheriting the ``tenant.viewer``
fence its ~90 sibling routes already use, and ``/api/diagnose`` lost the query
parameter entirely, so there is no longer a way to name a project outside the
path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from frisket.team.app import TeamConfig, create_team_app
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


def _team_app(tmp_path: Path) -> Any:
    async def send(_email: str, _link: str) -> bool:
        return True

    return create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="Diagnose Fence Desk",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send,
    )


def _login(client: TestClient, app: Any, email: str) -> None:
    claim_server(app, client=client)
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)


def _two_members_two_projects(tmp_path: Path) -> tuple[Any, TestClient, str, str]:
    """Alice holds viewer on project A only; Bob holds viewer on project B only."""

    app = _team_app(tmp_path)
    owner = TestClient(app)
    alice = TestClient(app)
    bob = TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(alice, app, "alice@example.com")
    _login(bob, app, "bob@example.com")

    project_a = owner.post("/api/projects", json={"name": "Alice Beat"}).json()["id"]
    project_b = owner.post("/api/projects", json={"name": "Bob Beat"}).json()["id"]
    for pid, email in (
        (project_a, "alice@example.com"),
        (project_b, "bob@example.com"),
    ):
        granted = owner.post(
            f"/api/projects/{pid}/members", json={"email": email, "role": "viewer"}
        )
        assert granted.status_code == 200, granted.text

    # The premise: each member really does hold a role on exactly one project.
    assert alice.get(f"/api/projects/{project_a}").status_code == 200
    assert alice.get(f"/api/projects/{project_b}").status_code == 403
    assert bob.get(f"/api/projects/{project_b}").status_code == 200

    return app, alice, project_a, project_b


def _plugin_health(body: dict[str, Any]) -> dict[str, Any]:
    probes = {**(body.get("core") or {}), **(body.get("info") or {})}
    return probes["plugin_health"]


def test_project_scoped_diagnose_runs_the_project_role_ladder(tmp_path: Path) -> None:
    app, alice, project_a, project_b = _two_members_two_projects(tmp_path)

    mine = alice.get(f"/api/projects/{project_a}/diagnose")
    assert mine.status_code == 200, mine.text
    assert _plugin_health(mine.json())["available"] is True

    theirs = alice.get(f"/api/projects/{project_b}/diagnose")
    assert theirs.status_code == 403, theirs.text
    # Not a 404: the ladder refuses on role, and the refusal body carries no
    # project state.
    assert "plugin_health" not in theirs.text


def test_bare_diagnose_cannot_name_a_project_at_all(tmp_path: Path) -> None:
    """The query parameter is gone, so it cannot smuggle a project past the gate."""

    app, alice, project_a, project_b = _two_members_two_projects(tmp_path)

    for target in (project_a, project_b, "no-such-project"):
        response = alice.get("/api/diagnose", params={"project_id": target})
        assert response.status_code == 200, response.text
        health = _plugin_health(response.json())
        assert health["available"] is False, (
            f"/api/diagnose?project_id={target} still opened a project"
        )
        assert health["summary"] == (
            "no project in scope (open a project to see plugin health)"
        )


# Bulk data-takeout routes: the whole project (incl. raw SQLite via mode=db),
# a full sheet, the work log (prompt text + before/after example cell
# values), or a whole embedding index. These were classified viewer-tier
# alongside the ordinary per-cell/per-row reads until 2026-08 -- a newsroom
# granting read access does not mean to grant a full data takeout, so the
# endpoint catalog (frisket.contracts.http.endpoint_catalog) now declares
# them reviewer-tier, one rung up. This is the same tenant.viewer/reviewer
# ladder the module docstring above describes; extended here rather than in
# a new file since this is the existing project-role-ladder-vs-catalog test.
#
# sheet_id/index_id are deliberately nonexistent: the role ladder in
# team/enforcement.py resolves ONLY from path_params["pid"], before the
# route handler ever looks up the sheet/index, so a fake id still proves the
# ladder's verdict (403 vs not-403) without needing real sheet/embedding
# fixtures. A reviewer therefore gets 200 (project/work-log exports, which
# need no other resource) or 404 (sheet/embedding routes, whose resource
# doesn't exist) -- either way, never 403.
_BULK_EXPORT_ROUTES: tuple[tuple[str, str], ...] = (
    ("export_project (bundle)", "/api/projects/{pid}/export"),
    ("export_project (db)", "/api/projects/{pid}/export?mode=db"),
    (
        "export_sheet_dataset",
        "/api/projects/{pid}/exports/sheets?sheet_id=999999&format=csv",
    ),
    ("export_work_log", "/api/projects/{pid}/export/work-log.md"),
    ("export_work_log_html", "/api/projects/{pid}/export/work-log.html"),
    ("export_work_log_pdf", "/api/projects/{pid}/export/work-log.pdf"),
    (
        "embedding_index_export_download",
        "/api/projects/{pid}/embeddings/v1/indexes/missing-index/export/csv",
    ),
    (
        "embedding_index_export_manifest",
        "/api/projects/{pid}/embeddings/v1/indexes/missing-index/export",
    ),
)


def test_bulk_export_routes_require_reviewer_not_viewer(tmp_path: Path) -> None:
    app = _team_app(tmp_path)
    owner = TestClient(app)
    viewer = TestClient(app)
    reviewer = TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(viewer, app, "viewer@example.com")
    _login(reviewer, app, "reviewer@example.com")

    pid = owner.post("/api/projects", json={"name": "Export Fence"}).json()["id"]
    for email, role in (
        ("viewer@example.com", "viewer"),
        ("reviewer@example.com", "reviewer"),
    ):
        granted = owner.post(
            f"/api/projects/{pid}/members", json={"email": email, "role": role}
        )
        assert granted.status_code == 200, granted.text

    for name, template in _BULK_EXPORT_ROUTES:
        path = template.format(pid=pid)
        refused = viewer.get(path)
        assert refused.status_code == 403, (
            f"{name}: a viewer must be refused ({path}), got "
            f"{refused.status_code}: {refused.text}"
        )
        allowed = reviewer.get(path)
        assert allowed.status_code != 403, (
            f"{name}: a reviewer must not be refused by the role ladder "
            f"({path}), got {allowed.status_code}: {allowed.text}"
        )
