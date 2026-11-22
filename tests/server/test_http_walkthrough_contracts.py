from __future__ import annotations


def test_walkthrough_catalog_is_backend_authored_and_typed(client) -> None:

    response = client.get("/api/walkthroughs")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "walkthroughs": [
            {"id": "project-ask", "badges": ["Project Ask", "Citations"]},
            {"id": "lawsuit-documents", "badges": ["PDFs", "OCR", "Review"]},
            {"id": "civic-ai-triage", "badges": ["AI triage", "Views", "Judge"]},
            {"id": "combine-values", "badges": ["Data cleanup", "Clustering"]},
            {"id": "council-audio", "badges": ["Audio", "Transcription"]},
            {"id": "rss-import", "badges": ["RSS", "Views", "Alerts"]},
            {
                "id": "multilingual-names",
                "badges": ["Translate", "Transliterate"],
            },
            {"id": "regex-extract", "badges": ["Regex", "Joins"]},
            {"id": "local-model-lab", "badges": ["Local AI"]},
        ]
    }

    schema = client.app.openapi()["paths"]["/api/walkthroughs"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/WalkthroughCatalogResponse"}
