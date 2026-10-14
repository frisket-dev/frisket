from __future__ import annotations

import json

import pytest

from frisket.contracts.models_gateway_roster import (
    InvalidModelsGatewayRoster,
    ModelsGatewayRoster,
    ModelsGatewayRosterEntry,
    models_gateway_roster_from_capabilities,
    parse_models_gateway_roster,
)
from frisket.execution.definitions import (
    MODELS_GATEWAY_TARGET_ID,
    build_static_targets,
)
from frisket.execution.models_gateway_roster import (
    InvalidManagedModelsGatewayConfig,
    resolve_managed_models_gateway_roster,
)


def test_deployment_roster_keeps_identity_and_discards_live_health() -> None:
    roster = models_gateway_roster_from_capabilities(
        {
            "service": "frisket-models",
            "engines": [
                {
                    "name": "paddleocr-vl",
                    "route": "/ocr",
                    "available": False,
                    "loaded": False,
                    "error": "worker is cold",
                    "models": ["PaddleOCR-VL"],
                },
                {
                    "name": "docling",
                    "route": "/to-markdown",
                    "available": True,
                    "loaded": True,
                    "error": None,
                },
            ],
        }
    )

    assert roster.to_payload() == {
        "schema_version": "frisket.models_gateway_roster.v1",
        "engines": [
            {"name": "paddleocr-vl", "route": "/ocr"},
            {"name": "docling", "route": "/to-markdown"},
        ],
    }
    assert parse_models_gateway_roster(roster.to_json()) == roster
    assert json.loads(roster.to_json()) == roster.to_payload()


def test_empty_deployment_roster_is_an_explicit_disabled_configuration() -> None:
    roster = parse_models_gateway_roster(
        '{"schema_version":"frisket.models_gateway_roster.v1","engines":[]}'
    )

    assert roster == ModelsGatewayRoster(())
    assert roster.as_catalog_capabilities() == {
        "configured": True,
        "managed_roster": True,
        "engines": [],
    }


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "[]",
        '{"schema_version":"frisket.models_gateway_roster.v2","engines":[]}',
        '{"schema_version":"frisket.models_gateway_roster.v1","engines":{}}',
        (
            '{"schema_version":"frisket.models_gateway_roster.v1","engines":['
            '{"name":"paddleocr-vl","route":"/ocr"},'
            '{"name":"paddleocr-vl","route":"/ocr"}]}'
        ),
    ],
)
def test_invalid_deployment_roster_refuses(value: str) -> None:
    with pytest.raises(InvalidModelsGatewayRoster, match="FRISKET_MODELS_ROSTER_JSON"):
        parse_models_gateway_roster(value)


def test_capability_export_rejects_entries_without_stable_identity() -> None:
    with pytest.raises(
        InvalidModelsGatewayRoster,
        match="require string name and route",
    ):
        models_gateway_roster_from_capabilities(
            {"engines": [{"name": "paddleocr-vl", "available": True}]}
        )


def test_managed_roster_filters_gateway_support_by_exact_route_and_name() -> None:
    roster = models_gateway_roster_from_capabilities(
        {
            "engines": [
                {"name": "paddleocr-vl", "route": "/ocr"},
                {"name": "paddleocr-vl", "route": "/to-markdown"},
                {"name": "unknown-engine", "route": "/ocr"},
            ]
        }
    )

    gateway = next(
        target
        for target in build_static_targets(managed_models_gateway_roster=roster)
        if target.id == MODELS_GATEWAY_TARGET_ID
    )

    assert [(support.capability, support.engine) for support in gateway.engines] == [
        ("ocr", "paddleocr-vl")
    ]


def test_managed_gateway_resolver_requires_connection_and_returns_roster() -> None:
    roster = resolve_managed_models_gateway_roster(
        {
            "FRISKET_MODELS_URL": "https://models.example.invalid",
            "FRISKET_MODELS_TOKEN": "models-token",
            "FRISKET_MODELS_ROSTER_JSON": (
                '{"schema_version":"frisket.models_gateway_roster.v1",'
                '"engines":[{"name":"paddleocr-vl","route":"/ocr"}]}'
            ),
        }
    )

    assert roster.engines == (
        ModelsGatewayRosterEntry(route="/ocr", name="paddleocr-vl"),
    )


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {
            "FRISKET_MODELS_URL": "https://models.example.invalid",
            "FRISKET_MODELS_TOKEN": "models-token",
        },
        {
            "FRISKET_MODELS_URL": "https://models.example.invalid",
            "FRISKET_MODELS_ROSTER_JSON": (
                '{"schema_version":"frisket.models_gateway_roster.v1","engines":[]}'
            ),
        },
        {
            "FRISKET_MODELS_URL": "https://models.example.invalid",
            "FRISKET_MODELS_TOKEN": "models-token",
            "FRISKET_MODELS_ROSTER_JSON": "not-json",
        },
    ],
)
def test_managed_gateway_resolver_refuses_incomplete_or_invalid_config(
    environment: dict[str, str],
) -> None:
    with pytest.raises(
        InvalidManagedModelsGatewayConfig,
        match="FRISKET_MODELS_ROSTER_JSON",
    ):
        resolve_managed_models_gateway_roster(environment)
