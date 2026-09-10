from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.ai.external_pricing import (
    CENSUS_US_ACS,
    GEOCODE_EXTERNAL_GEOCODER,
    GEOCODE_NOMINATIM_ROW,
)
from frisket.server.app import create_app


def test_admin_pricing_route_preserves_catalog_response_shape(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    client = TestClient(create_app(tmp_path / "ws"))

    response = client.get("/api/admin/pricing/external")

    assert response.status_code == 200
    catalog = response.json()
    assert GEOCODE_EXTERNAL_GEOCODER in catalog
    assert GEOCODE_NOMINATIM_ROW not in catalog
    assert CENSUS_US_ACS not in catalog
