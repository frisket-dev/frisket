"""A project created with a cover seed keeps it; one without keeps the cover
its id derives, so existing projects need no upgrade step."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _client(tmp_path) -> TestClient:
    return TestClient(create_app(tmp_path / "workspace"))


def test_create_stores_and_returns_the_cover_seed(tmp_path) -> None:
    client = _client(tmp_path)
    made = client.post("/api/projects", json={"name": "Notes", "cover_seed": "k3x9"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    assert made.json()["cover_seed"] == "k3x9"
    assert client.get(f"/api/projects/{pid}").json()["cover_seed"] == "k3x9"
    assert [row["cover_seed"] for row in client.get("/api/projects").json()] == ["k3x9"]
    manifest = json.loads(
        (tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json").read_text()
    )
    assert manifest["cover_seed"] == "k3x9"


def test_a_project_without_a_seed_reports_none(tmp_path) -> None:
    client = _client(tmp_path)
    made = client.post("/api/projects", json={"name": "Notes"})
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    assert made.json()["cover_seed"] is None
    assert client.get(f"/api/projects/{pid}").json()["cover_seed"] is None
    assert [row["cover_seed"] for row in client.get("/api/projects").json()] == [None]


@pytest.mark.parametrize("seed", ["", "x" * 33, "has space", "slash/", "ünï"])
def test_create_refuses_a_seed_outside_the_allowed_shape(tmp_path, seed) -> None:
    client = _client(tmp_path)
    refused = client.post("/api/projects", json={"name": "Notes", "cover_seed": seed})
    assert refused.status_code == 422, refused.text
    assert client.get("/api/projects").json() == []


def test_idempotent_create_treats_another_seed_as_another_payload(tmp_path) -> None:
    client = _client(tmp_path)
    headers = {"Idempotency-Key": "notes"}
    request = {"name": "Notes", "cover_seed": "aaaa"}
    first = client.post("/api/projects", json=request, headers=headers)
    again = client.post("/api/projects", json=request, headers=headers)
    assert first.status_code == again.status_code == 200, again.text
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["cover_seed"] == "aaaa"
    changed = client.post(
        "/api/projects", json={**request, "cover_seed": "bbbb"}, headers=headers
    )
    assert changed.status_code == 409, changed.text


def test_a_manifest_seed_that_is_not_a_seed_lists_as_none(tmp_path) -> None:
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Notes"}).json()["id"]
    path = tmp_path / "workspace" / f"{pid}.frisket" / "manifest.json"
    manifest = json.loads(path.read_text())
    path.write_text(json.dumps({**manifest, "cover_seed": "<script>"}))
    assert [row["cover_seed"] for row in client.get("/api/projects").json()] == [None]
