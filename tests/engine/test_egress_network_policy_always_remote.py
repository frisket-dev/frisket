"""Egress gate for always-remote action kinds.

Census demographics, ``media.fetch_url``, ``research.web_search``, and
web.capture_page each carry a static ``external:*`` tag on their CATALOG
``required_capabilities`` and must be gated pre-queue when the project's
effective network policy is "off". Each test sends the canonical native
request, so a ``network_disabled`` result can only come from
``ActionRunService._network_disabled_result``, the unified choke point in
``server/services/action_runs.py`` that covers both queued dispatch
(validate/enqueue) and ``web.capture_page``'s direct dispatch alike.

Neither ``test_egress_network_policy_bypass.py`` (geocode only),
``test_egress_network_policy_local_ok.py`` (classifier-level, not HTTP
dispatch), nor ``test_egress_network_policy_picker.py`` (UI hints, not
dispatch) exercise an actual POST to ``.../actions/v1/run`` for these four
kinds, so this file is the genuine coverage gap."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps
from frisket.ops.capture.url import StaticUrlFetchResult
from frisket.server.app import create_app


def _static_capture(url: str, **_kwargs: Any) -> StaticUrlFetchResult:
    return StaticUrlFetchResult(
        requested_url=url,
        final_url=url,
        status_code=200,
        headers={"content-type": "text/html"},
        body=b"<html><body>offline capture</body></html>",
        elapsed_ms=1,
    )


def _client(tmp_path) -> TestClient:
    return TestClient(
        create_app(
            tmp_path / "workspace",
            router=ModelRouter(cache=None, cache_mode="off"),
            executor_deps_factory=lambda _project_id, _request: ExecutorDeps(
                url_capture_fetcher=_static_capture
            ),
        )
    )


def _project_id(client: TestClient, name: str) -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _set_network(client: TestClient, project_id: str, mode: str) -> None:
    response = client.patch(f"/api/projects/{project_id}/network", json={"mode": mode})
    assert response.status_code == 200, response.text


def _run(client: TestClient, project_id: str, body: dict[str, Any]):
    return client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)


def _run_count(client: TestClient, project_id: str) -> int:
    project = client.app.state.workspace.get(project_id)
    return project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _assert_network_disabled(response, capability: str) -> None:
    payload = response.json()
    assert payload["status"] == "failed", payload
    assert len(payload["errors"]) == 1, payload
    assert payload["errors"][0]["code"] == "network_disabled", payload
    assert payload["errors"][0]["details"]["capability"] == capability, payload


def _assert_not_network_disabled(response) -> None:
    payload = response.json()
    codes = [e["code"] for e in payload.get("errors", [])]
    assert "network_disabled" not in codes, payload


def _census_action(key: str) -> dict[str, Any]:
    return {
        "action_id": "enrich.census_demographics",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": "point",
            "geography": "tract",
            "include_moe": False,
        },
        "idempotency_key": f"census_demographics@sha256:{key}",
    }


def _media_fetch_url_action(key: str) -> dict[str, Any]:
    return {
        "action_id": "media.fetch_url",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {"source": "url"},
        "output_names": {"media": "media"},
        "idempotency_key": f"media_fetch_url@sha256:{key}",
    }


def _web_search_action(key: str) -> dict[str, Any]:
    return {
        "action_id": "research.web_search",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "query": {"text": "US tariff impacts on {{country}} economy 2026"},
            "max_results": 2,
        },
        "output_names": {"search_results": "search_results"},
        "idempotency_key": f"research_web_search@sha256:{key}",
    }


def _capture_page_action(key: str) -> dict[str, Any]:
    return {
        "action_id": "web.capture_page",
        "scope": {"kind": "sheet_rows", "sheet_id": 1},
        "params": {
            "source": "url",
            "render_mode": "static",
        },
        "output_names": {"page": "capture"},
        "idempotency_key": f"web_capture_page@sha256:{key}",
    }


def _seed_census(client: TestClient, project_id: str) -> None:
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Places")
    columns = {"point": project.add_column(sheet_id, "point", type="geo_point")}
    project.add_rows(sheet_id, [{"point": {"lat": 40.0, "lon": -75.0}}], columns)


def _seed_urls(client: TestClient, project_id: str, sheet_name: str) -> None:
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet(sheet_name)
    columns = {"url": project.add_column(sheet_id, "url", type="link")}
    project.add_rows(sheet_id, [{"url": "https://example.com/a"}], columns)


def _seed_countries(client: TestClient, project_id: str) -> None:
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Countries")
    columns = {"country": project.add_column(sheet_id, "country", type="text")}
    project.add_rows(sheet_id, [{"country": "Japan"}], columns)


def test_census_demographics_gated_when_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    client = _client(tmp_path)
    pid = _project_id(client, "AR Census")
    _seed_census(client, pid)
    _set_network(client, pid, "off")

    response = _run(client, pid, _census_action("off"))
    _assert_network_disabled(response, "external:us_census_acs")
    assert _run_count(client, pid) == 0


def test_media_fetch_url_gated_when_off(tmp_path) -> None:
    client = _client(tmp_path)
    pid = _project_id(client, "AR Fetch")
    _seed_urls(client, pid, "Feed")
    _set_network(client, pid, "off")

    response = _run(client, pid, _media_fetch_url_action("off"))
    _assert_network_disabled(response, "external:media_download")
    assert _run_count(client, pid) == 0


def test_research_web_search_gated_when_off(tmp_path, monkeypatch) -> None:
    import ddgs

    class ForbiddenDDGS:
        def text(self, *_args, **_kwargs):
            raise AssertionError("network-off gate must run before DDGS")

    monkeypatch.setattr(ddgs, "DDGS", ForbiddenDDGS)
    client = _client(tmp_path)
    pid = _project_id(client, "AR WebSearch")
    _seed_countries(client, pid)
    _set_network(client, pid, "off")

    response = _run(client, pid, _web_search_action("off"))
    _assert_network_disabled(response, "external:web_search")
    assert _run_count(client, pid) == 0


def test_web_capture_page_gated_when_off(tmp_path) -> None:
    """web.capture_page is INTENTIONALLY_DIRECT (queue_policy.py) -- it never
    passes through MapRunner.validate_spec, so this exercises the unified
    ``_network_disabled_result`` choke point (server/services/action_runs.py)
    on the direct-dispatch path rather than the queued/validate path the
    other cases in this file cover."""
    client = _client(tmp_path)
    pid = _project_id(client, "AR CapturePage")
    _seed_urls(client, pid, "Pages")
    _set_network(client, pid, "off")

    response = _run(client, pid, _capture_page_action("off"))
    _assert_network_disabled(response, "external:url_capture")
    assert _run_count(client, pid) == 0


@pytest.mark.parametrize(
    ("seed_fn", "action_fn", "project_name"),
    [
        (_seed_census, _census_action, "AR Census On"),
        (
            lambda client, pid: _seed_urls(client, pid, "Feed"),
            _media_fetch_url_action,
            "AR Fetch On",
        ),
        (_seed_countries, _web_search_action, "AR WebSearch On"),
        (
            lambda client, pid: _seed_urls(client, pid, "Pages"),
            _capture_page_action,
            "AR CapturePage On",
        ),
    ],
)
def test_always_remote_kinds_not_gated_when_on(
    tmp_path, monkeypatch, seed_fn, action_fn, project_name
) -> None:
    """Sanity: the honest, well-formed request for each always-remote kind is
    not gate-blocked when policy is "on" (the effective default) -- proves
    the gate is keyed on policy, not a blanket refusal of these kinds."""
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    monkeypatch.setattr("frisket.ops.capture.url.url_is_safe", lambda _url: True)
    client = _client(tmp_path)
    pid = _project_id(client, project_name)
    seed_fn(client, pid)

    response = _run(client, pid, action_fn("on"))
    _assert_not_network_disabled(response)
