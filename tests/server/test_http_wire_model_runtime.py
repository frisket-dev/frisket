from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.contracts.http.models import HttpError
from frisket.server.app import create_app


def test_unhandled_server_error_uses_sanitized_typed_json_envelope(tmp_path) -> None:
    app = create_app(tmp_path / "workspace")

    @app.get("/api/_wire-error-probe")
    def wire_error_probe() -> None:
        raise RuntimeError("must-not-leak-wire-error-secret")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/_wire-error-probe")

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": "Internal Server Error"}
    HttpError.model_validate(response.json())
    assert "must-not-leak-wire-error-secret" not in response.text
