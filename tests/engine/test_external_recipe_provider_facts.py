"""Durable-fact closure for shipped paid non-LLM recipe effects.

These are action-level regressions: the provider adapter is stubbed one layer
below the recipe, while the real 402 echo, MapRunner result writer, receipt
builder, and durable ``model_calls`` table all run unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from executor_harness import run_action_with_confirmation
from frisket.ai.llm import ModelRouter
from frisket.contracts.action import Receipt
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore


def _router(handler: Any | None = None) -> ModelRouter:
    router = ModelRouter(cache=None, cache_mode="off", env_key_source="local")
    if handler is not None:
        router._client = httpx.AsyncClient(  # noqa: SLF001 - injected transport seam
            transport=httpx.MockTransport(handler)
        )
    return router


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _units(row: Any) -> dict[str, Any]:
    return json.loads(row["units"])


def _translate_action(sheet_id: int, engine: str, *, key: str) -> dict[str, Any]:
    """The typed hosted-translate request; consent is the 402 token echo."""

    return {
        "action_id": "map.translate",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["statement"],
            "engine": engine,
            "target_language": "Spanish",
        },
        "output_names": {"translation": "es"},
        "idempotency_key": key,
    }


def test_opencage_action_persists_one_fact_per_paid_request_and_receipt_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENCAGE_API_KEY", raising=False)
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    monkeypatch.setattr(
        Project,
        "secret_plaintext",
        lambda _project, name: (
            "test-opencage-key" if name == "OPENCAGE_API_KEY" else None
        ),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "geometry": {"lat": 35.7101, "lng": 139.8107},
                        "formatted": "Tokyo Skytree, Tokyo, Japan",
                        "confidence": 9,
                    }
                ]
            },
        )

    project = Project.create(tmp_path / "geocode.frisket", name="geocode facts")
    try:
        sheet_id = project.add_sheet("Places")
        column_id = project.add_column(sheet_id, "address", type="text")
        project.add_rows(
            sheet_id,
            [
                {"address": "Tokyo Skytree"},
                {"address": "Tokyo Tower"},
                {"address": None},
            ],
            {"address": column_id},
        )
        from frisket.engine.executor import run_action_spec

        action = {
            "action_id": "enrich.geocode",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
            "params": {"source": "address", "engine": "opencage"},
            "output_names": {"geo_point": "location"},
            "idempotency_key": "geocode-facts@sha256:stable",
        }
        router = _router(handler)
        gated = run_action_spec(
            project, action, project_id="project-geocode-facts", router=router
        )
        assert gated.status == "needs_confirmation", gated.errors
        assert calls == []
        assert RunResultStore(project).model_calls() == []
        action["confirmation"] = gated.errors[0].details["promise_set_hash"]
        result = run_action_spec(
            project,
            action,
            project_id="project-geocode-facts",
            router=router,
        )
        assert result.status == "completed", result.errors
        assert len(calls) == 2

        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == len(calls)
        assert {row["provider"] for row in facts} == {"opencage"}
        assert {row["engine"] for row in facts} == {"opencage"}
        assert {row["credential_source"] for row in facts} == {"project_key"}
        assert {_units(row)["requests"] for row in facts} == {1}
        assert {row["provider_cost_usd"] for row in facts} == {0.01}

        receipt = _receipt(project, result.receipt_id)
        assert receipt.provider_use[0]["request_count"] == len(facts)
        assert receipt.provider_use[0]["cost_actual"] == pytest.approx(
            sum(float(row["provider_cost_usd"]) for row in facts)
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    ("engine", "env_name", "response", "expected_provider"),
    [
        (
            "deepl",
            "DEEPL_API_KEY",
            {
                "translations": [
                    {
                        "text": "Hola",
                        "detected_source_language": "EN",
                        "billed_characters": 5,
                    }
                ]
            },
            "deepl",
        ),
        (
            "google_translate",
            "GOOGLE_TRANSLATE_API_KEY",
            {
                "data": {
                    "translations": [
                        {"translatedText": "Hola", "detectedSourceLanguage": "en"}
                    ]
                }
            },
            "google",
        ),
    ],
)
def test_hosted_translate_action_persists_character_fact_and_receipt_agrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    env_name: str,
    response: dict[str, Any],
    expected_provider: str,
) -> None:
    monkeypatch.setenv(env_name, "test-key:fx")
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response)

    project = Project.create(
        tmp_path / f"translate-{engine}.frisket", name=f"{engine} facts"
    )
    try:
        sheet_id = project.add_sheet("Text")
        column_id = project.add_column(sheet_id, "statement", type="text")
        project.add_rows(sheet_id, [{"statement": "Hello"}], {"statement": column_id})
        result = run_action_with_confirmation(
            project,
            _translate_action(
                sheet_id, engine, key=f"translate-{engine}-facts@sha256:stable"
            ),
            project_id=f"project-translate-{engine}-facts",
            router=_router(handler),
        )
        assert result.status == "completed", result.errors
        assert calls == 1

        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == calls
        fact = facts[0]
        assert fact["provider"] == expected_provider
        assert fact["engine"] == engine
        assert fact["credential_source"] == "local"
        assert _units(fact) == {"characters": 5, "requests": 1}
        assert fact["provider_cost_usd"] is not None

        receipt = _receipt(project, result.receipt_id)
        # Hosted translate is an external metered API: the receipt names the
        # engine/service explicitly and agrees with the durable fact's cost.
        assert receipt.provider_use == [
            {
                "provider": expected_provider,
                "engine": engine,
                "service": "translate",
                "external_api": True,
                "credential_source": "local",
                "request_count": 1,
                "model_call_count": 1,
                "cost_actual": pytest.approx(float(fact["provider_cost_usd"])),
            }
        ]
    finally:
        project.close()


def test_hosted_translate_unknown_price_stays_unknown_in_fact_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import frisket.ai.external_pricing as external_pricing

    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    monkeypatch.setattr(external_pricing, "external_unit_price_usd", lambda _key: None)
    project = Project.create(tmp_path / "translate-unknown.frisket", name="unknown")
    try:
        sheet_id = project.add_sheet("Text")
        column_id = project.add_column(sheet_id, "statement", type="text")
        project.add_rows(sheet_id, [{"statement": "Hello"}], {"statement": column_id})
        result = run_action_with_confirmation(
            project,
            _translate_action(
                sheet_id, "deepl", key="translate-unknown-facts@sha256:stable"
            ),
            project_id="project-translate-unknown-facts",
            router=_router(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "translations": [
                            {
                                "text": "Hola",
                                "detected_source_language": "EN",
                                "billed_characters": 5,
                            }
                        ]
                    },
                )
            ),
        )
        assert result.status == "completed", result.errors
        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == 1
        assert facts[0]["provider_cost_usd"] is None
        assert facts[0]["cost_source"] == "unknown"
        receipt = _receipt(project, result.receipt_id)
        assert receipt.provider_use[0]["model_call_count"] == 1
        assert receipt.provider_use[0]["cost_actual"] is None
    finally:
        project.close()


def test_hosted_translate_empty_paid_response_still_persists_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Output validation can fail after a provider has already billed."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-key:fx")
    project = Project.create(tmp_path / "translate-empty.frisket", name="empty")
    try:
        sheet_id = project.add_sheet("Text")
        column_id = project.add_column(sheet_id, "statement", type="text")
        project.add_rows(sheet_id, [{"statement": "Hello"}], {"statement": column_id})
        result = run_action_with_confirmation(
            project,
            _translate_action(
                sheet_id, "deepl", key="translate-empty-facts@sha256:stable"
            ),
            project_id="project-translate-empty-facts",
            router=_router(
                lambda _request: httpx.Response(
                    200,
                    json={
                        "translations": [
                            {
                                "text": "",
                                "detected_source_language": "EN",
                                "billed_characters": 5,
                            }
                        ]
                    },
                )
            ),
        )
        assert result.status == "failed"
        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == 1
        assert facts[0]["provider_cost_usd"] == pytest.approx(0.000125)
        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "failed"
        assert receipt.provider_use[0]["model_call_count"] == 1
        assert receipt.provider_use[0]["cost_actual"] == pytest.approx(0.000125)
    finally:
        project.close()


@pytest.mark.parametrize(
    ("engine", "env_name", "expected_provider"),
    [
        ("deepl", "DEEPL_API_KEY", "deepl"),
        ("google_translate", "GOOGLE_TRANSLATE_API_KEY", "google"),
    ],
)
def test_hosted_translate_http_response_failure_persists_unknown_request_fact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    env_name: str,
    expected_provider: str,
) -> None:
    """A concrete provider response cannot become a confident zero-call receipt."""
    monkeypatch.setenv(env_name, "test-key:fx")
    responses = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal responses
        responses += 1
        return httpx.Response(503, json={"message": "temporarily unavailable"})

    project = Project.create(
        tmp_path / f"translate-{engine}-503.frisket", name=f"{engine} 503"
    )
    try:
        sheet_id = project.add_sheet("Text")
        column_id = project.add_column(sheet_id, "statement", type="text")
        project.add_rows(sheet_id, [{"statement": "Hello"}], {"statement": column_id})
        result = run_action_with_confirmation(
            project,
            _translate_action(
                sheet_id, engine, key=f"translate-{engine}-503@sha256:stable"
            ),
            project_id=f"project-translate-{engine}-503",
            router=_router(handler),
        )
        assert result.status == "failed"
        assert responses == 1

        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == responses
        fact = facts[0]
        assert fact["provider"] == expected_provider
        assert fact["engine"] == engine
        assert fact["credential_source"] == "local"
        assert fact["provider_cost_usd"] is None
        assert fact["cost_source"] == "unknown"
        assert _units(fact) == {"requests": 1}

        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "failed"
        assert receipt.provider_use[0]["provider"] == expected_provider
        assert receipt.provider_use[0]["engine"] == engine
        assert receipt.provider_use[0]["model_call_count"] == responses
        assert receipt.provider_use[0]["request_count"] == responses
        assert receipt.provider_use[0]["cost_actual"] is None
    finally:
        project.close()


@pytest.mark.parametrize(
    ("reported_cost", "catalog_price", "expected_cost"),
    [(0.03, 0.01, 0.03), (None, None, None)],
)
def test_datalab_to_markdown_action_persists_page_fact_and_receipt_agrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported_cost: float | None,
    catalog_price: float | None,
    expected_cost: float | None,
) -> None:
    import frisket.ai.external_pricing as external_pricing
    from frisket.engine.executor import run_action_spec
    from frisket.ops.integrations import datalab

    monkeypatch.setenv("DATALAB_API_KEY", "test-datalab-key")
    monkeypatch.setattr(
        external_pricing, "external_unit_price_usd", lambda _key: catalog_price
    )
    calls = 0

    async def fake_convert(
        *args: Any, **kwargs: Any
    ) -> tuple[dict[str, Any], float | None]:
        nonlocal calls
        calls += 1
        # The real datalab_convert mints an accepted-job fact and fires
        # on_accepted before polling; the typed converter now requires it.
        on_accepted = kwargs.get("on_accepted")
        if on_accepted is not None:
            on_accepted(
                datalab._accepted_submission_accounting(
                    {
                        "request_check_url": "https://datalab.test/check/facts",
                        "request_id": "facts-req",
                    },
                    capability="document.convert",
                    credential_source="local",
                )
            )
        return {"markdown": "# Converted", "page_count": 3}, reported_cost

    monkeypatch.setattr(datalab, "datalab_convert", fake_convert)
    project = Project.create(tmp_path / "datalab.frisket", name="datalab facts")
    try:
        sheet_id = project.add_sheet("Documents")
        column_id = project.add_column(sheet_id, "doc", type="file")
        blob = project.add_blob(
            b"%PDF-1.4 fake",
            filename="document.pdf",
            mime="application/pdf",
        )
        row_ids = project.add_rows(
            sheet_id,
            [
                {
                    "doc": media_cell(
                        blob, mime="application/pdf", filename="document.pdf"
                    )
                }
            ],
            {"doc": column_id},
        )
        action = {
            "action_id": "media.to_markdown",
            "scope": {
                "kind": "sheet_rows",
                "sheet_id": sheet_id,
                "row_ids": row_ids,
            },
            "params": {
                "source": "doc",
                "engine": "datalab",
            },
            "output_names": {"markdown": "markdown"},
            "idempotency_key": "datalab-facts@sha256:stable",
        }
        router = _router(lambda _request: httpx.Response(500))
        quote = run_action_spec(
            project, action, project_id="project-datalab-facts", router=router
        )
        assert quote.status == "needs_confirmation", quote.errors
        assert calls == 0
        action["confirmation"] = quote.errors[0].details["promise_set_hash"]
        result = run_action_spec(
            project, action, project_id="project-datalab-facts", router=router
        )
        assert result.status == "completed", result.errors
        assert calls == 1

        facts = RunResultStore(project).model_calls(result.run_id)
        assert len(facts) == calls
        fact = facts[0]
        assert fact["provider"] == "datalab"
        assert fact["engine"] == "datalab"
        assert fact["credential_source"] == "local"
        assert _units(fact) == {"pages": 3, "requests": 1}
        assert fact["provider_reported_cost_usd"] == reported_cost
        assert fact["provider_cost_usd"] == expected_cost

        receipt = _receipt(project, result.receipt_id)
        assert receipt.provider_use == [
            {
                "provider": "datalab",
                "model": "datalab",
                "engine": "datalab",
                "external_api": True,
                "service": "document.convert",
                "operation_call_count": 1,
                "model_call_count": 1,
                "cost_actual": expected_cost,
            }
        ]
    finally:
        project.close()
