from fastapi.testclient import TestClient
from frisket.server.app import create_app
from frisket.server.routes.skills import register_skill_routes
from frisket.server.services.skills import SkillLibrary
from frisket.team.enforcement import protect_core_app

ACTOR = {"id": 7, "email": "proof@example.com", "org_id": 1, "auth": "session"}


class Access:
    role = "viewer"
    found = True
    asked = None

    def resolve_project_for_user_id(self, user_id, slug, **kwargs):
        return {"org_id": 1, "storage_org_id": 1, "slug": slug} if self.found else None

    def can_on_project(self, org_id, slug, user_id, need):
        self.asked = need
        return ("viewer", "reviewer", "editor", "owner").index(self.role) >= (
            "viewer",
            "reviewer",
            "editor",
            "owner",
        ).index(need)


def test_evaluation_roles_and_project_isolation(tmp_path):
    app = create_app(
        tmp_path / "core", serve_spa=False, enable_provider_config=False, edition="team"
    )
    register_skill_routes(
        app,
        library=SkillLibrary(tmp_path / "core"),
        require_manager=lambda _request: None,
    )
    access = Access()
    protect_core_app(
        app=app,
        resolve_user=lambda _request: ACTOR,
        admin_emails=lambda: set(),
        project_access=access,
    )
    base = "/api/projects/proof/evaluation"
    checks = [
        ("POST", "/score", "viewer"),
        ("POST", "/cell", "viewer"),
        ("GET", "/shelf?sheet_id=1&action_id=map.test", "viewer"),
        ("PUT", "/judgment", "reviewer"),
        ("POST", "/download.csv", "reviewer"),
        ("POST", "/settings.json", "reviewer"),
        ("GET", "/review.csv?run_id=1", "reviewer"),
        ("POST", "/links", "editor"),
        ("PUT", "/rules/1", "editor"),
        ("POST", "/expected-answer", "editor"),
        ("POST", "/capture", "editor"),
        ("POST", "/shelf", "editor"),
    ]
    with TestClient(app, raise_server_exceptions=False) as client:
        for role in ("viewer", "reviewer", "editor"):
            access.role = role
            for method, suffix, need in checks:
                access.asked = None
                response = client.request(method, base + suffix, json={})
                assert access.asked == need, (role, suffix, access.asked)
                denied = ("viewer", "reviewer", "editor").index(role) < (
                    "viewer",
                    "reviewer",
                    "editor",
                ).index(need)
                assert (response.status_code == 403) == denied, (
                    role,
                    suffix,
                    response.status_code,
                    response.text,
                )
        access.found = False
        for method, suffix, _ in checks:
            response = client.request(method, base + suffix, json={})
            assert response.status_code == 403, (
                suffix,
                response.status_code,
                response.text,
            )
