import pytest
from fastapi.testclient import TestClient

from frisket.ai.external_pricing import (
    CENSUS_US_ACS,
    GEOCODE_EXTERNAL_GEOCODER,
    GEOCODE_NOMINATIM_ROW,
    estimate_external_cost,
    external_pricing_catalog,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.engine.store import Project
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from frisket.execution.resolve_for_action import resolve_for_action
from frisket.execution.resolver import Refusal
from frisket.server.app import create_app


def _catalog_hints(client: TestClient, project_id=None) -> dict[str, dict]:
    prefix = f"/api/projects/{project_id}" if project_id else "/api"
    response = client.get(f"{prefix}/actions/v1/catalog")
    assert response.status_code == 200, response.text
    payload = response.json()
    return {entry["kind"]: entry.get("ui_hints") or {} for entry in payload["actions"]}


def test_catalog_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("FRISKET_GEOCODE_USD_PER_ROW", raising=False)

    catalog = external_pricing_catalog()
    assert catalog[GEOCODE_EXTERNAL_GEOCODER]["unit_price_usd"] == pytest.approx(0.01)
    assert GEOCODE_NOMINATIM_ROW not in catalog
    assert CENSUS_US_ACS not in catalog
    assert estimate_external_cost(GEOCODE_EXTERNAL_GEOCODER, 1802)["cost"] == (
        pytest.approx(18.02)
    )

    monkeypatch.setenv("FRISKET_GEOCODE_USD_PER_ROW", "0.02")
    assert estimate_external_cost(GEOCODE_EXTERNAL_GEOCODER, 3)["cost"] == (
        pytest.approx(0.06)
    )

    monkeypatch.setenv("FRISKET_GEOCODE_USD_PER_ROW", "not-a-decimal")
    with pytest.raises(ValueError, match="FRISKET_GEOCODE_USD_PER_ROW"):
        external_pricing_catalog()


def test_catalog_rejects_negative_and_nonfinite_env_prices(monkeypatch):
    # Retired free-public-API rate knobs cannot resurrect a fictional price.
    monkeypatch.setenv("FRISKET_CENSUS_DEMOGRAPHICS_USD_PER_ROW", "-0.01")
    assert CENSUS_US_ACS not in external_pricing_catalog()

    monkeypatch.setenv("FRISKET_GEOCODE_USD_PER_ROW", "NaN")
    with pytest.raises(ValueError, match="non-negative finite"):
        external_pricing_catalog()


def test_typed_program_estimates_use_catalog(tmp_path, monkeypatch):
    project = Project.create(tmp_path / "pricing.frisket")
    sheet_id = project.add_sheet("Places")
    address = project.add_column(sheet_id, "address", type="text")
    point = project.add_column(sheet_id, "point", type="geo_point")
    row_ids = project.add_rows(
        sheet_id,
        [{"address": "Oxford", "point": {"lat": 51.75, "lon": -1.25}}] * 3,
        {"address": address, "point": point},
    )

    def estimate(action_id, source, count):
        bound = BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get(action_id),
            ActionRequest(
                action_id=action_id,
                scope=SheetRows(sheet_id=sheet_id, row_ids=row_ids[:count]),
                params={"source": source},
                idempotency_key="pricing-catalog",
            ),
        )
        plan = build_typed_map_rows_plan(project, bound)
        spec = plan.spec_dict()
        resolved = resolve_for_action(
            project,
            spec,
            plan.program,
            composition=open_execution_composition(
                project, ModelRouter(), ExecutionCompositionContext.direct()
            ),
        )
        assert resolved is not None and not isinstance(resolved, Refusal), resolved
        return plan.program.estimate(project, spec, None, resolution=resolved)

    try:
        _assert_program_estimates(monkeypatch, estimate)
    finally:
        project.close()


def _assert_program_estimates(monkeypatch, estimate):
    monkeypatch.setenv("OPENCAGE_API_KEY", "test-key")
    monkeypatch.setenv("FRISKET_GEOCODE_USD_PER_ROW", "0.123")
    opencage = estimate("enrich.geocode", "address", 2)
    assert opencage["cost"] == pytest.approx(0.246)
    assert opencage["pricing_key"] == GEOCODE_EXTERNAL_GEOCODER
    assert opencage["engine"] == "opencage"
    assert opencage["cost_source"] == "pricing_data"

    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    monkeypatch.setenv("FRISKET_NOMINATIM_USD_PER_ROW", "0.007")
    nominatim = estimate("enrich.geocode", "address", 2)
    assert nominatim == {
        "cost": 0.0,
        "engine": "nominatim",
        "cost_source": "free_public_api",
    }

    monkeypatch.setenv("FRISKET_CENSUS_DEMOGRAPHICS_USD_PER_ROW", "0.004")
    monkeypatch.setenv("CENSUS_API_KEY", "test-key")
    census = estimate("enrich.census_demographics", "point", 3)
    assert census == {
        "cost": 0.0,
        "engine": "us_census_acs",
        "cost_source": "free_public_api",
    }


@pytest.mark.parametrize("project_scoped", [False, True], ids=["global", "project"])
def test_api_exposes_external_pricing_catalog(tmp_path, monkeypatch, project_scoped):
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    with TestClient(create_app(tmp_path / "ws")) as client:
        project_id = None
        if project_scoped:
            response = client.post("/api/projects", json={"name": "Pricing"})
            assert response.status_code == 200, response.text
            project_id = response.json()["id"]
        catalog = client.get("/api/admin/pricing/external").json()
        assert GEOCODE_EXTERNAL_GEOCODER in catalog
        assert GEOCODE_NOMINATIM_ROW not in catalog
        assert CENSUS_US_ACS not in catalog

        hints = _catalog_hints(client, project_id)
        assert "pricing" not in hints["enrich.geocode"]
        assert (
            hints["enrich.geocode"]["pricing_options"]["opencage"]["key"]
            == GEOCODE_EXTERNAL_GEOCODER
        )
        assert set(hints["enrich.geocode"]["pricing_options"]) == {"opencage"}
        assert hints["enrich.geocode"]["cost_source"] == "free_public_api"
        assert hints["enrich.geocode"]["cost_source_options"] == {
            "nominatim": "free_public_api"
        }
        assert "pricing" not in hints["enrich.census_demographics"]
        assert hints["enrich.census_demographics"]["cost_source"] == "free_public_api"
        engines = {entry["id"]: entry for entry in hints["enrich.geocode"]["engines"]}
        assert set(engines) == {"opencage", "nominatim"}
        assert engines["opencage"]["available"] is False
        assert engines["opencage"]["pricing"]["key"] == GEOCODE_EXTERNAL_GEOCODER
        assert engines["nominatim"]["available"] is True
        assert engines["nominatim"]["billable"] is False
        assert "pricing" not in engines["nominatim"]
        census_engines = hints["enrich.census_demographics"]["engines"]
        assert [engine["id"] for engine in census_engines] == ["us_census_acs"]
        assert census_engines[0]["billable"] is False
        assert "pricing" not in census_engines[0]
        monkeypatch.setenv("OPENCAGE_API_KEY", "test-key")
        hints = _catalog_hints(client, project_id)
        assert hints["enrich.geocode"]["pricing"]["key"] == GEOCODE_EXTERNAL_GEOCODER
        assert "cost_source" not in hints["enrich.geocode"]
        assert hints["enrich.geocode"]["cost_source_options"] == {
            "nominatim": "free_public_api"
        }
        engines = {entry["id"]: entry for entry in hints["enrich.geocode"]["engines"]}
        assert engines["opencage"]["available"] is True
        assert engines["opencage"]["pricing"]["key"] == GEOCODE_EXTERNAL_GEOCODER
