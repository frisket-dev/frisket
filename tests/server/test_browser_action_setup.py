"""Browser setup is an independent prerequisite, not a lost action draft."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from frisket.engine.jobs.engine_setup import CHROMIUM_SETUP_REF
from frisket.engine.jobs.queue import open_queue
from frisket.runtime import chromium
from frisket.server.app import create_app
from frisket.server.services.action_param_validation import ActionParamValidationService


def test_browser_preflight_preserves_outputs_and_refreshes_after_setup(
    tmp_path, monkeypatch
):
    queue = open_queue(workspace=tmp_path)
    try:
        service = ActionParamValidationService(
            SimpleNamespace(root=tmp_path, queue=queue, edition="solo")
        )
        monkeypatch.setattr(chromium, "chromium_library_available", lambda: True)
        monkeypatch.setattr(chromium, "chromium_ready", lambda: False)
        action = {"kind": "web.capture_screenshot", "params": {"source": "url"}}
        missing = service.validate_params("project", action)
        assert missing["diagnostics"] == {}
        assert missing["logical_outputs"] == [
            {
                "key": "screenshot",
                "column_type": "image",
            }
        ]
        assert missing["runtime_setup"]["setup_ref"] == CHROMIUM_SETUP_REF
        assert missing["runtime_setup"]["can_start"] is True
        monkeypatch.setattr(chromium, "chromium_ready", lambda: True)
        ready = service.validate_params("project", action)
        assert ready["runtime_setup"] is None
        assert ready["logical_outputs"] == missing["logical_outputs"]
    finally:
        queue.close()


def test_browser_setup_only_applies_to_local_browser_rendering(monkeypatch):
    def unexpected_probe():
        raise AssertionError("unrelated actions must not probe local Chromium")

    monkeypatch.setattr(chromium, "chromium_library_available", unexpected_probe)
    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    assert service._runtime_setup("web.capture_page", {"render_mode": "static"}) is None
    assert service._runtime_setup("map.template", {}) is None
    for edition in ("cloud", "team"):
        service = ActionParamValidationService(SimpleNamespace(edition=edition))
        assert service._runtime_setup("web.capture_screenshot", {}) is None


def test_missing_python_library_has_instructions_not_an_impossible_install(monkeypatch):
    monkeypatch.setattr(chromium, "chromium_library_available", lambda: False)
    service = ActionParamValidationService(SimpleNamespace(edition="solo"))
    for kind, params in (
        ("web.capture_screenshot", {}),
        ("web.capture_page", {"render_mode": "playwright"}),
    ):
        setup = service._runtime_setup(kind, params)
        assert setup["kind"] == "instructions"
        assert "frisket-data[browser]" in setup["steps"][0]


def test_chromium_setup_route_deduplicates_and_does_not_execute_actions(tmp_path):
    # No lifespan worker: inspect the actual queued operation without downloading.
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    first = client.post(
        "/api/providers/models/setup", json={"setup_ref": CHROMIUM_SETUP_REF}
    )
    assert first.status_code == 202, first.text
    again = client.post(
        "/api/providers/models/setup", json={"setup_ref": CHROMIUM_SETUP_REF}
    )
    assert again.status_code == 202, again.text
    assert again.json()["deduplicated"] is True
    assert again.json()["pull"]["id"] == first.json()["pull"]["id"]
    assert first.json()["pull"]["operation_kind"] == "engine_setup"
    assert first.json()["pull"]["display_name"] == "Browser tools (Chromium)"
