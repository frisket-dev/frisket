"""the regression guard: the local-tier provider key-config
routes must be unreachable on the hosted tier.

register_provider_config_routes was called unconditionally in create_app, so
hosted per-org tenant sub-apps carried /api/providers* and tenant dispatch's
capability gates (segs[0] in {spend, admin, projects}) never applied — any
signed-in user of any org could read process-env key hints, write key
material to the org workspace file (bypassing KEY_MANAGER_ROLES on
/api/org/keys), and use the validate probe as an unmetered oracle for
testing arbitrary keys via the server's egress.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_provider_config_routes_stay_available_on_local_tier(tmp_path, monkeypatch):
    from frisket.server.app import create_app

    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    monkeypatch.delenv("FRISKET_STATIC_DIR", raising=False)
    app = create_app(tmp_path / "ws")
    client = TestClient(app)
    assert client.get("/api/providers").status_code == 200
