from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_project_data_management_retention_contract_lists_supported_evidence_values(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Data Contract"}).json()
    pid = created["id"]
    manifest_path = tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json"

    initial = client.get(f"/api/projects/{pid}/retention")

    assert initial.status_code == 200, initial.text
    assert initial.json() == {
        "schemaVersion": "frisket.project_retention_policy.v1",
        "default_evidence": "compactable",
        "pin_evidence_by_default": False,
        "no_compact": False,
        "supported_default_evidence": ["compactable", "pinned", "materialized"],
    }

    updated = client.patch(
        f"/api/projects/{pid}/retention",
        json={"default_evidence": "materialized", "no_compact": True},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["default_evidence"] == "materialized"
    assert updated.json()["supported_default_evidence"] == [
        "compactable",
        "pinned",
        "materialized",
    ]
    manifest = json.loads(manifest_path.read_text())
    assert manifest["retention"] == {
        "default_evidence": "materialized",
        "pin_evidence_by_default": False,
        "no_compact": True,
    }

    unsupported = client.patch(
        f"/api/projects/{pid}/retention",
        json={"default_evidence": "manifest"},
    )

    assert unsupported.status_code == 400
    assert unsupported.json()["detail"] == "unsupported default_evidence: manifest"
    assert client.get(f"/api/projects/{pid}/retention").json()["default_evidence"] == (
        "materialized"
    )


def test_project_data_management_compact_contract_reports_destructive_summary(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Compact Contract"}).json()
    pid = created["id"]

    client.patch(f"/api/projects/{pid}/retention", json={"no_compact": True})
    skipped = client.post(f"/api/projects/{pid}/compact")

    assert skipped.status_code == 200, skipped.text
    assert skipped.json() == {
        "skipped": True,
        "reason": "project_no_compact",
        "results_pruned": 0,
        "blobs_removed": 0,
        "bytes_freed": 0,
        "db_bytes_before": skipped.json()["db_bytes_before"],
        "db_bytes_after": skipped.json()["db_bytes_before"],
        "db_bytes_reclaimed": 0,
    }

    client.patch(f"/api/projects/{pid}/retention", json={"no_compact": False})
    compacted = client.post(f"/api/projects/{pid}/compact")

    assert compacted.status_code == 200, compacted.text
    body = compacted.json()
    assert body["skipped"] is False
    assert body["reason"] is None
    for key in (
        "results_pruned",
        "blobs_removed",
        "bytes_freed",
        "db_bytes_before",
        "db_bytes_after",
        "db_bytes_reclaimed",
    ):
        assert isinstance(body[key], int)
