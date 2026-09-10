"""Picker/catalog UX filter for the egress gate.

UX only — the authoritative gate is at validate/dispatch/replay. These prove
the picker stops OFFERING what dispatch would reject: remote providers leave
the provider catalog, hosted-tier engines read unavailable with a policy (not
credential) reason, and always-remote kinds carry ui_hints.network_disabled.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server import provider_config
from frisket.server.app import create_app
from frisket.engine.store import Project


def test_build_provider_catalog_network_off_keeps_only_local(tmp_path) -> None:
    catalog = provider_config.build_provider_catalog(
        tmp_path / "ws", {}, network_off=True
    )
    ids = [p["id"] for p in catalog["providers"]]
    assert ids == []
    assert catalog["network"] == "off"


def test_build_provider_catalog_default_is_unfiltered(tmp_path) -> None:
    catalog = provider_config.build_provider_catalog(tmp_path / "ws", {})
    ids = {p["id"] for p in catalog["providers"]}
    assert ids == {"anthropic", "openai", "gemini", "openrouter"}
    assert "network" not in catalog


def test_providers_route_threads_project_network_policy(tmp_path) -> None:
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
        )
    )
    pid = client.post("/api/projects", json={"name": "Picker"}).json()["id"]

    full = client.get("/api/providers", params={"project_id": pid}).json()
    assert {p["id"] for p in full["providers"]} == {
        "anthropic",
        "openai",
        "gemini",
        "openrouter",
    }

    client.patch(f"/api/projects/{pid}/network", json={"mode": "off"})
    filtered = client.get("/api/providers", params={"project_id": pid}).json()
    assert filtered["providers"] == []
    assert filtered["network"] == "off"

    # Instance-scoped call (no project) stays unfiltered.
    instance = client.get("/api/providers").json()
    assert {p["id"] for p in instance["providers"]} == {
        "anthropic",
        "openai",
        "gemini",
        "openrouter",
    }

    assert (
        client.get("/api/providers", params={"project_id": "nope"}).status_code == 404
    )


@pytest.fixture
def off_project(tmp_path):
    p = Project.create(tmp_path / "p.frisket")
    p.set_network_policy(mode="off")
    yield p
    p.close()


def _hints_payload(project):
    from frisket.server.action_catalog_hints import (
        project_action_catalog_payload_with_launcher_hints,
    )

    return project_action_catalog_payload_with_launcher_hints(
        project, sidecar_capabilities={}
    )


def test_hosted_engines_read_policy_unavailable_when_off(off_project) -> None:
    payload = _hints_payload(off_project)
    translate = next(a for a in payload["actions"] if a["kind"] == "map.translate")
    engines = {e["id"]: e for e in translate["ui_hints"]["engines"]}
    for hosted in ("deepl", "google_translate"):
        assert engines[hosted]["available"] is False
        assert "network setting is off" in engines[hosted]["error"]
    # Local engines keep their own availability semantics (whatever they are
    # on this machine) and never carry the network error.
    assert "network setting is off" not in (engines["opus_mt"].get("error") or "")

    ocr = next(a for a in payload["actions"] if a["kind"] == "media.ocr")
    ocr_engines = {e["id"]: e for e in ocr["ui_hints"]["engines"]}
    assert ocr_engines["datalab"]["available"] is False
    assert "network setting is off" in ocr_engines["datalab"]["error"]


def test_legacy_always_remote_kinds_carry_network_disabled_hint(off_project) -> None:
    payload = _hints_payload(off_project)
    by_kind = {a["kind"]: a for a in payload["actions"]}
    # Typed actions are merged at the HTTP catalog boundary after this legacy
    # launcher-hint projection. Their authoritative network refusal is covered
    # by test_egress_network_policy_always_remote.py.
    for kind in (
        "enrich.geocode",
        "enrich.census_demographics",
        "media.fetch_url",
        "web.capture_page",
    ):
        assert by_kind[kind]["ui_hints"].get("network_disabled") is True, kind
    # A local-only kind never carries the hint.
    assert "network_disabled" not in (by_kind["map.translate"].get("ui_hints") or {})


def test_no_network_hints_under_default_on(tmp_path) -> None:
    project = Project.create(tmp_path / "on.frisket")
    try:
        payload = _hints_payload(project)
        by_kind = {a["kind"]: a for a in payload["actions"]}
        assert "network_disabled" not in (
            by_kind["enrich.geocode"].get("ui_hints") or {}
        )
        translate = by_kind["map.translate"]
        engines = {e["id"]: e for e in translate["ui_hints"]["engines"]}
        assert "network setting is off" not in (engines["deepl"].get("error") or "")
    finally:
        project.close()
