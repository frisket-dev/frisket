from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_work_log_export_routes_preserve_download_contract(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    pid = client.post("/api/projects", json={"name": "Export Test"}).json()["id"]

    cases = (
        (
            "md",
            "text/markdown",
            'filename="Export-Test-work-log.md"',
            b"# Work log: Export Test",
        ),
        (
            "html",
            "text/html",
            'filename="Export-Test-work-log.html"',
            b"Work log: Export Test",
        ),
        (
            "pdf",
            "application/pdf",
            'filename="Export-Test-work-log.pdf"',
            b"%PDF-",
        ),
    )

    for extension, content_type, filename, body_marker in cases:
        response = client.get(f"/api/projects/{pid}/export/work-log.{extension}")

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(content_type)
        assert filename in response.headers["content-disposition"]
        assert response.content
        assert body_marker in response.content
