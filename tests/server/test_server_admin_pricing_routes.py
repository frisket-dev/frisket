from __future__ import annotations


from frisket.ai.external_pricing import (
    CENSUS_US_ACS,
    GEOCODE_EXTERNAL_GEOCODER,
    GEOCODE_NOMINATIM_ROW,
)


def test_admin_pricing_route_preserves_catalog_response_shape(
    client, monkeypatch
) -> None:
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)

    response = client.get("/api/admin/pricing/external")

    assert response.status_code == 200
    catalog = response.json()
    assert GEOCODE_EXTERNAL_GEOCODER in catalog
    assert GEOCODE_NOMINATIM_ROW not in catalog
    assert CENSUS_US_ACS not in catalog
