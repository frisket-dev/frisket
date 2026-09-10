"""Settings browser routes must refresh into the SPA, not the API 404.

Red-first for product-friction-settings-routes-context-v1 (2026-07-04).
The bug report came from hosted deep links, but the fallback primitive is shared
with the local tier; this pins both mounts while keeping /api outside the SPA
catch-all.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


SETTINGS_BROWSER_ROUTES = [
    "/settings/personal/profile",
    "/settings/organization/ai-providers",
    "/settings/organization/api-keys",
    "/p/case-files/settings/project/data-management",
]


def _make_static_dir(root: Path) -> Path:
    static_dir = root / "dist"
    static_dir.mkdir(parents=True)
    (static_dir / "index.html").write_text(
        "<html><head></head><body>frisket settings app</body></html>",
        encoding="utf-8",
    )
    (static_dir / "asset.txt").write_text("asset", encoding="utf-8")
    return static_dir


def _assert_settings_routes_serve_spa(client: TestClient) -> None:
    for route in SETTINGS_BROWSER_ROUTES:
        response = client.get(route)
        assert response.status_code == 200, route
        assert "frisket settings app" in response.text
        assert response.headers["cache-control"] == "no-cache, must-revalidate"


def _assert_api_routes_do_not_serve_spa(client: TestClient) -> None:
    missing_api = client.get("/api/no-such-settings-static-fallback")
    assert missing_api.status_code in {401, 404}
    assert "frisket settings app" not in missing_api.text
    assert "text/html" not in missing_api.headers.get("content-type", "")


def test_local_static_mount_serves_settings_deep_links_and_preserves_api(
    tmp_path, monkeypatch
):
    static_dir = _make_static_dir(tmp_path)
    monkeypatch.setenv("FRISKET_NO_WORKER", "1")
    app = create_app(tmp_path / "workspace", static_dir=static_dir)
    client = TestClient(app)

    _assert_settings_routes_serve_spa(client)

    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert "text/html" not in health.headers.get("content-type", "")
    _assert_api_routes_do_not_serve_spa(client)
