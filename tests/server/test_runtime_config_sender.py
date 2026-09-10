from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app

# ---------------------------------------------------------------------------
# Local server: GET /api/config carries an honest (null) sender -- the local
# tier never sends email, so there is nothing to report.


class TestLocalServerRuntimeConfigSender:
    def test_default_workspace_reports_no_sender_identity(self, tmp_path):
        client = TestClient(create_app(tmp_path / "workspace"))
        resp = client.get("/api/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["email_from_address"] is None
        assert body["email_from_name"] is None
        # cache_mode/live_calls_possible are untouched by this slice: the
        # default posture is replay, honestly reported as live-call-capable
        # (a replay miss falls through to a live call).
        assert body["cache_mode"] == "replay"
        assert body["live_calls_possible"] is True
        assert body["in_container"] is False


# ---------------------------------------------------------------------------
# GET /api/config reports the install posture, so Settings can name the
# mechanism that changes FRISKET_CACHE_MODE on THIS install instead of
# hardcoding the compose one for everybody.


class TestRuntimeConfigInstallPosture:
    def test_container_override_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRISKET_IN_CONTAINER", "1")
        client = TestClient(create_app(tmp_path / "workspace"))
        assert client.get("/api/config").json()["in_container"] is True

    def test_direct_launch_reports_no_container(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FRISKET_IN_CONTAINER", "false")
        client = TestClient(create_app(tmp_path / "workspace"))
        assert client.get("/api/config").json()["in_container"] is False


# ---------------------------------------------------------------------------
# Hosted app: GET /api/config carries HostedConfig.email_from_address/name


def _hosted_env(monkeypatch, tmp_path, **extra: str) -> None:
    monkeypatch.setenv("FRISKET_DATABASE_URL", f"sqlite:///{tmp_path}/control.db")
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRISKET_ALLOWED_EMAILS", "owner@example.com")
    monkeypatch.setenv("FRISKET_BASE_URL", "http://test")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
