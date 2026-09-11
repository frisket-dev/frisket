from __future__ import annotations

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_walkthrough_catalog_is_backend_authored_and_typed(tmp_path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))

    response = client.get("/api/walkthroughs")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "walkthroughs": [
            {"id": "regex-extract", "badges": ["Regex", "Joins"]},
            {"id": "lawsuit-documents", "badges": ["PDFs", "AI extraction"]},
            {"id": "council-audio", "badges": ["Audio", "Transcription"]},
            {"id": "civic-ai-triage", "badges": ["AI extraction"]},
            {"id": "rss-import", "badges": ["RSS", "AI classification"]},
            {"id": "combine-values", "badges": ["Data cleanup"]},
            {
                "id": "multilingual-names",
                "badges": ["Multilingual text", "Data cleanup"],
            },
            {"id": "local-model-lab", "badges": ["Local AI"]},
        ]
    }

    schema = client.app.openapi()["paths"]["/api/walkthroughs"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/WalkthroughCatalogResponse"}
