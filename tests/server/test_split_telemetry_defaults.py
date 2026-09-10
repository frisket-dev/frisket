from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.operability.telemetry import (
    ProductTelemetryEvent,
    build_product_telemetry_runtime,
    byte_bucket,
    column_bucket,
    duration_bucket,
    row_bucket,
)
from frisket.server.app import create_app


pytestmark = pytest.mark.gap

MONTHLY_ID = "a" * 64


class RecordingDestination:
    app_id = "test-app"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.signals: list[dict[str, Any]] = []

    def send(self, signal: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("destination unavailable")
        self.signals.append(signal)


def _body(event_type: str, properties: dict[str, str]) -> dict[str, Any]:
    return {"monthly_id": MONTHLY_ID, "type": event_type, "properties": properties}


def test_product_telemetry_bucket_edges_are_coarse_and_closed() -> None:
    assert [
        duration_bucket(value)
        for value in (
            0,
            999,
            1_000,
            9_999,
            10_000,
            59_999,
            60_000,
            599_999,
            600_000,
            None,
        )
    ] == [
        "lt_1s",
        "lt_1s",
        "1_10s",
        "1_10s",
        "10_60s",
        "10_60s",
        "1_10m",
        "1_10m",
        "gte_10m",
        "unknown",
    ]
    assert [
        byte_bucket(value)
        for value in (0, 1, 1024**2 - 1, 1024**2, 100 * 1024**2, 1024**3, None)
    ] == ["zero", "lt_1mb", "lt_1mb", "1_100mb", "100mb_1gb", "gte_1gb", "unknown"]
    assert [
        row_bucket(value)
        for value in (0, 1, 999, 1_000, 99_999, 100_000, 999_999, 1_000_000, None)
    ] == [
        "zero",
        "1_999",
        "1_999",
        "1k_99k",
        "1k_99k",
        "100k_999k",
        "100k_999k",
        "gte_1m",
        "unknown",
    ]
    assert [column_bucket(value) for value in (0, 1, 5, 6, 20, 21, 100, 101, None)] == [
        "zero",
        "1_5",
        "1_5",
        "6_20",
        "6_20",
        "21_100",
        "21_100",
        "gte_101",
        "unknown",
    ]
    for value in (-1, True):
        with pytest.raises(ValueError):
            row_bucket(value)


def test_event_schema_rejects_arbitrary_fields_values_and_success_failures() -> None:
    event = ProductTelemetryEvent("App.opened", {"platform": "linux"})
    assert event.context == "installation"
    for properties in (
        {"platform": "linux", "filename": "sources.csv"},
        {"platform": "private free text"},
    ):
        with pytest.raises(ValueError):
            ProductTelemetryEvent("App.opened", properties)
    with pytest.raises(ValueError):
        ProductTelemetryEvent(
            "Source.created",
            {
                "sourceKind": "rss",
                "requestDuration": "lt_1s",
                "result": "success",
                "failureCategory": "network",
            },
        )


def test_routes_add_trusted_fields_and_sensitive_projects_send_nothing(
    tmp_path,
) -> None:
    destination = RecordingDestination()
    client = TestClient(create_app(tmp_path, product_telemetry_destination=destination))
    config = client.get("/api/config")
    assert config.status_code == 200
    assert config.json()["product_telemetry_available"] is True

    opened = client.post(
        "/api/telemetry/events", json=_body("App.opened", {"platform": "linux"})
    )
    assert opened.status_code == 204
    assert destination.signals[0]["clientUser"] == MONTHLY_ID
    assert destination.signals[0]["payload"]["Frisket.edition"] == "solo"

    ordinary = client.post(
        "/api/projects", json={"name": "Ordinary", "sensitive": False}
    ).json()["id"]
    sensitive = client.post(
        "/api/projects", json={"name": "Protected", "sensitive": True}
    ).json()["id"]
    project_opened = _body("Project.opened", {})
    assert (
        client.post(
            f"/api/projects/{ordinary}/telemetry/events", json=project_opened
        ).status_code
        == 204
    )
    assert len(destination.signals) == 2
    assert (
        client.post(
            f"/api/projects/{sensitive}/telemetry/events", json=project_opened
        ).status_code
        == 204
    )
    assert len(destination.signals) == 2


def test_route_context_and_arbitrary_content_fail_closed(tmp_path) -> None:
    destination = RecordingDestination()
    client = TestClient(create_app(tmp_path, product_telemetry_destination=destination))
    pid = client.post("/api/projects", json={"name": "Ordinary"}).json()["id"]
    assert (
        client.post(
            "/api/telemetry/events", json=_body("Project.opened", {})
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/projects/{pid}/telemetry/events",
            json=_body("App.opened", {"platform": "linux"}),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/telemetry/events",
            json=_body("App.opened", {"platform": "linux", "prompt": "secret"}),
        ).status_code
        == 422
    )
    assert destination.signals == []


def test_destination_failure_is_dropped_and_env_off_is_absolute(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = RecordingDestination(fail=True)
    client = TestClient(
        create_app(tmp_path / "failing", product_telemetry_destination=failing)
    )
    assert (
        client.post(
            "/api/telemetry/events", json=_body("App.opened", {"platform": "linux"})
        ).status_code
        == 204
    )

    monkeypatch.setenv("FRISKET_TELEMETRY", "off")
    recording = RecordingDestination()
    disabled = TestClient(
        create_app(tmp_path / "disabled", product_telemetry_destination=recording)
    )
    assert disabled.get("/api/config").json()["product_telemetry_available"] is False
    assert (
        disabled.post(
            "/api/telemetry/events", json=_body("App.opened", {"platform": "linux"})
        ).status_code
        == 204
    )
    assert recording.signals == []


def test_fresh_runtime_is_inert_without_complete_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FRISKET_TELEMETRY", raising=False)
    monkeypatch.delenv("FRISKET_TELEMETRYDECK_NAMESPACE", raising=False)
    monkeypatch.delenv("FRISKET_TELEMETRYDECK_APP_ID", raising=False)
    assert build_product_telemetry_runtime().available is False
    monkeypatch.setenv("FRISKET_TELEMETRYDECK_NAMESPACE", "com.frisket")
    assert build_product_telemetry_runtime().available is False
    monkeypatch.setenv("FRISKET_TELEMETRYDECK_APP_ID", "test-app")
    assert build_product_telemetry_runtime().available is True
