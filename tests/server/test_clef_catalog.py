from __future__ import annotations

import pytest

from frisket.contracts.classification import classification_options
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import OperatorBorne
from frisket.execution.provider import CompositionFacts, ExecutionComposition
from frisket.server.action_catalog_hints import (
    _recipe_engines,
    project_action_catalog_launcher_hints,
)


@pytest.fixture(autouse=True)
def _isolated_classifier_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRISKET_DISABLE_LOCAL_EMBED", "1")
    monkeypatch.delenv("FRISKET_ENABLE_PROVIDERLESS_CLASSIFY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setattr(
        "frisket.engine._workers.classifier_artifacts.classifier_runtime_present",
        lambda: False,
    )
    monkeypatch.setattr(
        "frisket.engine._workers.classifier_artifacts.classifier_ready",
        lambda engine: False,
    )


def _engines(capabilities=None, project=None, action_kind="map.classify"):
    return {
        row["id"]: row
        for row in _recipe_engines(action_kind, capabilities or {}, project=project)
    }


@pytest.mark.parametrize("action_kind", ["map.classify", "map.extract"])
def test_clef_catalog_projects_supported_fields_without_a_model_selector(
    action_kind: str,
) -> None:
    engines = _engines(action_kind=action_kind)
    for engine_id in ("clef", "clef-flash"):
        row = engines[engine_id]
        assert row["classification_options"] == classification_options(engine_id)
        assert (
            "Always selects an answer, even when the input is inconclusive."
            in row["description"]
        )
        assert "Text only" in row["description"]
        assert "Citations are not supported" in row["description"]
        assert "models" not in row
        assert row["available"] is False
    assert engines["clef"]["tier"] == "hosted"
    assert engines["clef"]["billable"] is True
    assert engines["clef-flash"]["tier"] == "sidecar"
    assert engines["clef-flash"]["billable"] is False
    assert "FRISKET_MODELS_TOKEN" in engines["clef-flash"]["error"]
    assert "CLOUDFLARE_ACCOUNT_ID" in engines["clef"]["error"]
    if action_kind == "map.classify":
        assert engines["llm"]["classification_options"] == classification_options("llm")
    else:
        # Extract's generative fields include date/list/json and no justification.
        assert "classification_options" not in engines["llm"]


def test_extract_catalog_does_not_probe_unrelated_classifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_probe(*args, **kwargs):
        pytest.fail("Extract must not probe unrelated local classifiers")

    monkeypatch.setattr("frisket.semantic.local_embedder", unexpected_probe)
    for function in (
        "classifier_runtime_present",
        "classifier_ready",
        "classifier_artifact",
    ):
        monkeypatch.setattr(
            f"frisket.engine._workers.classifier_artifacts.{function}", unexpected_probe
        )
    assert set(_engines(action_kind="map.extract")) == {"llm", "clef", "clef-flash"}


@pytest.mark.parametrize("route", ["/classify", "/chat"])
def test_clef_flash_requires_classify_capability(route: str) -> None:
    row = _engines(
        {
            "configured": True,
            "engines": [{"name": "clef-flash", "route": route, "available": True}],
        }
    )["clef-flash"]
    assert row["available"] is (route == "/classify")


def test_clef_flash_preserves_worker_unavailable_reason() -> None:
    row = _engines(
        {
            "configured": True,
            "engines": [
                {
                    "name": "clef-flash",
                    "route": "/classify",
                    "available": False,
                    "error": "Install the classify extra.",
                }
            ],
        }
    )["clef-flash"]
    assert row["available"] is False
    assert row["error"] == "Install the classify extra."


@pytest.mark.parametrize("token_source", ["environment", "project"])
def test_clef_uses_execution_credential_sources(
    monkeypatch: pytest.MonkeyPatch, token_source: str
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "a" * 32)

    class ProjectSecrets:
        def secret_plaintext(self, name):
            return "fixture-token" if name == "CLOUDFLARE_API_TOKEN" else None

    if token_source == "environment":
        monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fixture-token")
    row = _engines(project=ProjectSecrets() if token_source == "project" else None)[
        "clef"
    ]
    assert row["available"] is True
    assert "fixture-token" not in repr(row)


def test_clef_malformed_account_is_unavailable_without_leaking_its_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "bad-account")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "fixture-token")
    row = _engines()["clef"]
    assert row["available"] is False
    assert "CLOUDFLARE_ACCOUNT_ID" in row["error"]
    assert "bad-account" not in repr(row)
    assert "fixture-token" not in repr(row)


@pytest.mark.parametrize("action_kind", ["map.classify", "map.extract"])
def test_clef_catalog_uses_request_composition_connection(action_kind: str) -> None:
    provider = StaticExecutionTargetProvider(
        env={"CLOUDFLARE_ACCOUNT_ID": "a" * 32, "CLOUDFLARE_API_TOKEN": "fixture-token"}
    )
    composition = ExecutionComposition(
        facts=CompositionFacts(edition="open", funding=OperatorBorne()),
        provider=provider,
        credential_use_context=CredentialUseContext(),
    )
    hints = project_action_catalog_launcher_hints(
        {},
        execution_composition=composition,
        action_kinds=frozenset({action_kind}),
    )
    clef = next(row for row in hints[action_kind]["engines"] if row["id"] == "clef")
    assert clef["available"] is True
    assert clef["target_id"] == "cloudflare-clef"


def test_clef_flash_advertisement_cannot_bypass_missing_gateway_connection() -> None:
    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=StaticExecutionTargetProvider(env={}),
        credential_use_context=CredentialUseContext(),
    )
    hints = project_action_catalog_launcher_hints(
        {
            "configured": True,
            "engines": [
                {"name": "clef-flash", "route": "/classify", "available": True}
            ],
        },
        execution_composition=composition,
        action_kinds=frozenset({"map.classify"}),
    )
    clef = next(
        row for row in hints["map.classify"]["engines"] if row["id"] == "clef-flash"
    )
    assert clef["available"] is False
    assert clef["target_id"] == "models-gateway"


@pytest.mark.parametrize("worker_state", ["missing", "unavailable", "available"])
def test_live_gateway_preserves_clef_flash_capability_readiness(
    worker_state: str,
) -> None:
    composition = ExecutionComposition(
        facts=CompositionFacts(),
        provider=StaticExecutionTargetProvider(
            env={
                "FRISKET_MODELS_URL": "http://127.0.0.1:8500",
                "FRISKET_MODELS_TOKEN": "fixture-token",
            }
        ),
        credential_use_context=CredentialUseContext(),
    )
    engines = (
        []
        if worker_state == "missing"
        else [
            {
                "name": "clef-flash",
                "route": "/classify",
                "available": worker_state == "available",
                "error": "Install the classify-clef extra.",
            }
        ]
    )
    hints = project_action_catalog_launcher_hints(
        {"configured": True, "engines": engines},
        execution_composition=composition,
        action_kinds=frozenset({"map.classify"}),
    )
    clef = next(
        row for row in hints["map.classify"]["engines"] if row["id"] == "clef-flash"
    )
    assert clef["available"] is (worker_state == "available")
    if worker_state == "missing":
        assert "not advertised" in clef["error"]
    elif worker_state == "unavailable":
        assert clef["error"] == "Install the classify-clef extra."
