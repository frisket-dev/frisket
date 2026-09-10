"""Error remediation and in-app Diagnose backend contract.

Three things under test:
1. ``frisket.llm.remediation.classify_llm_error`` — the seam where the two
   named, recoverable LLM failure classes (missing provider key, Ollama
   unreachable) are translated, not string-matched later in the UI.
2. ``MapRunner._prepare`` raises a typed ``MissingProviderKey`` at
   run-confirm time (before any row work happens) for a live run whose
   model's provider has no configured adapter — and does NOT for a
   ``replay_strict`` golden-cache run, which never
   touches a live adapter regardless of configured keys.
3. The `/api/diagnose` route serves the SAME probe module `frisket doctor`
   (cli.py) prints from — frisket.diagnostics, extracted once so neither
   caller duplicates the logic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from frisket.engine.executor import ExecutorDeps
from frisket.ai.llm import (
    LLMError,
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    classify_llm_error,
    request_key,
)
from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
from typed_model_fixtures import estimate_model_run
from typed_model_fixtures import prepare_model_run
from typed_model_fixtures import run_with_output_claim
from frisket.ai.llm.remediation import (
    LOCAL_TRANSCRIBE_UNSUPPORTED,
    MISSING_PROVIDER_KEY,
    MODEL_ERROR,
    MODEL_NOT_INSTALLED,
    OLLAMA_UNREACHABLE,
)
from typed_model_fixtures import model_plan
from frisket.sdk.ops.transcribe_engines import LocalTranscribeUnsupportedError
from frisket.engine.runner import MapRunner, MissingProviderKey
from frisket.engine.runner.validation import ClaimsGate
from frisket.execution.attempt_authority import UnroutedOnlyAuthority
from frisket.server.app import create_app
from frisket.engine.store import Project

PROJECT_ID = "project-error-diagnose"


# ---------------------------------------------------------------------------
# classify_llm_error — the CLASS-level translation seam


def test_classify_missing_provider_key_names_provider_and_settings():
    exc = LLMError("no adapter for provider 'anthropic' (configured: ollama)")
    remediated = classify_llm_error(exc, provider="anthropic")
    assert remediated.code == MISSING_PROVIDER_KEY
    assert "anthropic" in remediated.message
    assert "Settings" in remediated.message
    assert remediated.details["provider"] == "anthropic"


def test_classify_ollama_connection_refused_names_the_url():
    exc = LLMError("transport error", retryable=True, transport_kind="connect")
    remediated = classify_llm_error(
        exc,
        provider="ollama",
        endpoint_origin="http://localhost:11434",
        model="ollama/@desk/qwen3:8b",
    )
    assert remediated.code == OLLAMA_UNREACHABLE
    assert "http://localhost:11434" in remediated.message
    # local-openai-compat-server-v1: class, not brand
    assert "pick another model" in remediated.message
    assert remediated.details["endpoint_origin"] == "http://localhost:11434"
    assert remediated.details["endpoint_id"] == "desk"
    assert remediated.details["model"] == "ollama/@desk/qwen3:8b"


def test_classify_non_ollama_connection_error_stays_generic_model_error():
    """The Ollama-specific copy is only for ollama — some other
    OpenAI-compatible base_url (a self-hosted LM Studio, say) refusing a
    connection is not "Ollama isn't running"."""
    expected_message: str | None = None
    try:
        raise httpx.ConnectError("Connection refused")
    except httpx.ConnectError as cause:
        try:
            raise LLMError(f"transport error: {cause}", retryable=True) from cause
        except LLMError as exc:
            expected_message = str(exc)
            remediated = classify_llm_error(exc, provider="openrouter")
    assert remediated.code == MODEL_ERROR
    assert remediated.message == expected_message


def test_classify_local_transcribe_unsupported_gets_a_typed_code():
    """The borrower's typed "unsupported in this deployment shape" error
    (raised by ``ops/transcribe.py`` when the local endpoint's ``edge_auth``
    says the split-host Caddy front door doesn't allowlist the transcription
    route) must classify to its own actionable code — not fall into the
    generic ``model_error`` bucket carrying its raw message like any other
    ``RuntimeError``."""
    exc = LocalTranscribeUnsupportedError(
        "local-server transcription isn't supported through an authenticated front door"
    )
    remediated = classify_llm_error(exc, provider="ollama")
    assert remediated.code == LOCAL_TRANSCRIBE_UNSUPPORTED
    assert "transcription" in remediated.message.lower()
    assert "cloud provider" in remediated.message.lower()


def test_classify_generic_model_error_keeps_the_original_message():
    exc = LLMError("rate limited", status=429, retryable=True)
    remediated = classify_llm_error(exc, provider="anthropic")
    assert remediated.code == MODEL_ERROR
    assert remediated.message == str(exc)


# ---------------------------------------------------------------------------
# model_not_installed — local-server 404 with structured not-found evidence.
# The predicate is the provider's machine-readable reason
# (LLMError.provider_code, parsed from the
# error body by adapters._raise_for_status), never the prose message.


@pytest.mark.parametrize(
    "body",
    [
        # Live shapes captured 2026-07-16 from Ollama /v1/chat/completions and
        # /v1/embeddings respectively — `type` carries the evidence, `code` is
        # null, and only the prose message differs.
        {
            "error": {
                "message": "model 'definitely-not-installed:latest' not found",
                "type": "not_found_error",
                "param": None,
                "code": None,
            }
        },
        {
            "error": {
                "message": 'model "also-missing" not found, try pulling it first',
                "type": "not_found_error",
                "param": None,
                "code": None,
            }
        },
    ],
    ids=["chat_completions", "embeddings"],
)
def test_raise_for_status_extracts_ollama_not_found_evidence(body):
    from frisket.ai.llm.adapters import _raise_for_status

    resp = httpx.Response(
        404,
        json=body,
        request=httpx.Request("POST", "http://localhost:11434/v1/chat/completions"),
    )
    with pytest.raises(LLMError) as excinfo:
        _raise_for_status(resp)
    assert excinfo.value.status == 404
    assert excinfo.value.retryable is False
    assert excinfo.value.provider_code == "not_found_error"


def test_classify_ollama_missing_model_names_model_and_pull_command():
    exc = LLMError(
        "provider returned 404: {'error': {...}}",
        status=404,
        retryable=False,
        provider_code="not_found_error",
    )
    remediated = classify_llm_error(
        exc,
        provider="ollama",
        endpoint_origin="http://localhost:11434",
        model="ollama/@desktop/qwen3:0.6b",
    )
    assert remediated.code == MODEL_NOT_INSTALLED
    # Bare model name (no provider prefix) — it's what `ollama pull` takes.
    assert "qwen3:0.6b" in remediated.message
    assert "ollama pull qwen3:0.6b" in remediated.message
    assert "http://localhost:11434" in remediated.message
    # Class, not brand: Ollama appears only as the command example.
    assert "pick another model" in remediated.message
    assert remediated.details["model"] == "ollama/@desktop/qwen3:0.6b"
    assert remediated.details["endpoint_id"] == "desktop"
    assert remediated.details["pull_command"] == "ollama pull qwen3:0.6b"
    assert remediated.details["endpoint_origin"] == "http://localhost:11434"


def test_classify_ollama_missing_model_without_model_name_still_typed():
    """The seam may not know the requested model (older callers don't pass
    it) — the class is still knowable from the typed evidence; only the
    copyable command needs the name."""
    exc = LLMError(
        "provider returned 404: {'error': {...}}",
        status=404,
        retryable=False,
        provider_code="not_found_error",
    )
    remediated = classify_llm_error(
        exc, provider="ollama", endpoint_origin="http://localhost:11434"
    )
    assert remediated.code == MODEL_NOT_INSTALLED
    assert "http://localhost:11434" in remediated.message
    assert "pull_command" not in remediated.details


def test_classify_ollama_404_without_structured_evidence_stays_model_error():
    """An HTML 404 (proxy misroute, wrong path) parses no provider_code —
    ambiguous, so it stays model_error per this module's no-guessing
    contract."""
    exc = LLMError(
        "provider returned 404: 404 page not found", status=404, retryable=False
    )
    remediated = classify_llm_error(
        exc, provider="ollama", endpoint_origin="http://localhost:11434"
    )
    assert remediated.code == MODEL_ERROR


def test_remediation_details_never_carry_a_token_value():
    """Redaction/no-echo pin: ``classify_llm_error`` takes an endpoint origin,
    never a
    token — this pins that a router whose adapter is built from a
    token-bearing endpoint config still only ever hands this seam the
    origin, so a planted canary token cannot appear anywhere in the
    remediated details even for a connection failure against that origin."""
    canary_token = "sk-canary-remediation-must-never-echo"
    router = ModelRouter(
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="heavy",
                display_name="Heavy",
                origin="https://llm.heavy.internal",
                inference_token=canary_token,
                source="env",
            ),
        )
    )
    exc = LLMError("transport error", retryable=True, transport_kind="connect")
    remediated = classify_llm_error(
        exc,
        provider="ollama",
        endpoint_origin=router.local_endpoints[0].origin,
    )
    assert canary_token not in remediated.message
    assert canary_token not in json.dumps(remediated.details)


def test_classify_cloud_provider_missing_model_stays_model_error():
    """OpenAI's `model_not_found` 404 is a different genre (typo'd model id,
    no pull remediation exists) — the install copy is local-server-only."""
    exc = LLMError(
        "provider returned 404: model does not exist",
        status=404,
        retryable=False,
        provider_code="model_not_found",
    )
    remediated = classify_llm_error(exc, provider="openai", model="openai/gpt-5.6")
    assert remediated.code == MODEL_ERROR


# ---------------------------------------------------------------------------
# MapRunner: missing-key blocks at run-confirm time, not per-row


def _seed_sheet(project: Project) -> tuple[int, dict[str, int]]:
    sheet_id = project.add_sheet("data")
    cols = {"text": project.add_column(sheet_id, "text")}
    project.add_rows(sheet_id, [{"text": "hello there"}], cols)
    return sheet_id, cols


def _classify_spec(sheet_id: int, model: str = "anthropic/claude-haiku-4-5") -> dict:
    return {
        "action_kind": "map.classify",
        "model": model,
        "sheet_id": sheet_id,
        "input_columns": ["text"],
        "context": "test",
        "fields": [
            {"name": "relevance", "type": "score", "description": "0-10 relevance"}
        ],
    }


def _exactly_confirmed_spec(runner: MapRunner, spec: dict[str, Any]) -> dict[str, Any]:
    """Complete the same quote/echo handshake as a production client."""
    with pytest.raises(ClaimsGate) as gate:
        prepare_model_run(runner, spec)
    assert gate.value.promise_set_hash
    return {
        **spec,
        "consented_promise_set_hash": gate.value.promise_set_hash,
    }


def test_prepare_run_blocks_before_any_row_work_when_key_is_missing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FRISKET_COST_CONSENT_USD", "0")
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id, _cols = _seed_sheet(project)
        spec = _classify_spec(sheet_id)
        router = ModelRouter()  # no keys configured for any live provider
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        confirmed_spec = _exactly_confirmed_spec(runner, spec)
        with pytest.raises(MissingProviderKey) as excinfo:
            prepare_model_run(runner, confirmed_spec, confirmed=True)
        assert excinfo.value.provider == "anthropic"
        # no run row was ever created for this attempt — it never queued
        assert project.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    finally:
        project.close()


def test_replay_strict_golden_cache_run_is_not_treated_as_missing_key(tmp_path):
    """cache_mode=replay_strict (tests/test_golden.py's default) never calls
    a live adapter — a keyless router replaying a committed cache is not a
    misconfiguration and must keep working."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id, _cols = _seed_sheet(project)
        spec = _classify_spec(sheet_id)
        cache = ResponseCache(tmp_path / "c.db")
        recipe = model_plan(spec).program
        call = recipe.render({"text": "hello there"}, spec)
        req = LLMRequest(
            model=spec["model"],
            messages=call.messages,
            schema=call.schema,
            max_tokens=call.max_tokens,
        )
        cache.put(
            request_key(req, recipe.version),
            LLMResponse(
                content=None,
                data={"relevance": 7},
                tokens_in=10,
                tokens_out=2,
                cost=0.0001,
                model=spec["model"],
            ),
        )
        router = ModelRouter(cache=cache, cache_mode="replay_strict")  # no keys
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        progress = asyncio.run(run_with_output_claim(runner, spec, confirmed=True))
        assert progress.done and progress.failed == 0 and progress.completed == 1
    finally:
        project.close()


def test_configured_local_model_never_hits_missing_provider_key(tmp_path):
    """An explicitly configured endpoint is a keyless-capable local adapter;
    its known-zero run needs no cost consent and fails on reachability, not keys."""
    project = Project.create(tmp_path / "p.frisket")
    try:
        sheet_id, _cols = _seed_sheet(project)
        spec = _classify_spec(sheet_id, model="ollama/@desktop/llama3")
        router = ModelRouter(
            max_retries=0,
            local_endpoints=(
                LocalModelEndpointConfig(
                    endpoint_id="desktop",
                    display_name="Desktop",
                    origin="http://127.0.0.1:1",
                    source="local_file",
                ),
            ),
        )
        runner = MapRunner(project, router, authority=UnroutedOnlyAuthority(project))
        assert estimate_model_run(runner, spec)["cost"] == 0.0
        try:
            progress = asyncio.run(
                run_with_output_claim(
                    runner,
                    spec,
                )
            )
        except MissingProviderKey:
            pytest.fail("ollama model incorrectly treated as a missing key")
        assert progress.done and progress.failed == 1
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Reserved map-runner action path (executor.action_lifecycle): a typed
# ActionError, not a raw exception string, reaches the caller.


def _map_classify_action(sheet_id: int, *, idempotency_key: str) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "anthropic/claude-haiku-4-5",
            "context": "Classify city news stories for an accountability desk.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "infrastructure"],
                    "description": "Primary reporting beat.",
                },
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": idempotency_key,
    }


def _seed_classify_project(project_path: Path) -> int:
    project = Project.create(project_path, name="Map Classify Missing Key")
    try:
        sheet_id = project.add_sheet("Stories")
        columns = {
            "story": project.add_column(sheet_id, "story", type="text"),
        }
        project.add_rows(
            sheet_id,
            [{"story": "City hall awarded a no-bid contract"}],
            columns,
        )
        return sheet_id
    finally:
        project.close()


def test_map_classify_missing_provider_key_is_a_typed_run_confirm_error(tmp_path):
    from tests.execution_composition_helpers import open_attempt_authority
    from executor_harness import (
        run_action_with_confirmation as run_action_with_exact_confirmation,
    )

    project_path = tmp_path / "classify-missing-key.frisket"
    sheet_id = _seed_classify_project(project_path)
    action = _map_classify_action(
        sheet_id, idempotency_key="map_classify@sha256:missing-key"
    )

    class NoKeyMapRunner(MapRunner):
        def _prepare(self, *_args: Any, **_kwargs: Any) -> Any:
            raise MissingProviderKey("anthropic")

    project = Project(project_path)
    try:
        failed = run_action_with_exact_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                map_runner_factory=lambda project, router: NoKeyMapRunner(
                    project,
                    router,
                    authority=open_attempt_authority(project),
                )
            ),
        )
        assert failed.status == "failed"
        assert failed.errors[0].code == "missing_provider_key"
        assert "anthropic" in failed.errors[0].message
        assert failed.errors[0].details.get("provider") == "anthropic"
        # no stuck running reservation left behind
        stuck = project.db.execute(
            "SELECT status FROM receipts WHERE idempotency_key=?",
            ("map_classify@sha256:missing-key",),
        ).fetchone()
        assert stuck is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# /api/diagnose: same probe module as `frisket doctor`


def test_diagnose_route_reports_providers_local_endpoints_and_static_assets(tmp_path):
    client = TestClient(create_app(tmp_path / "ws"))
    resp = client.get("/api/diagnose")
    assert resp.status_code == 200
    body = resp.json()
    assert "healthy" in body
    assert body["core"]["project_store"]["ok"] is True
    info = body["info"]
    assert "configured" in info["model_providers"]
    assert info["local_model_endpoints"] == {
        "configured": 0,
        "reachable": 0,
        "endpoints": [],
    }
    assert "summary" in info["static_assets"]


def test_diagnose_local_probe_receives_the_caller_router_not_a_bare_one(
    monkeypatch,
):
    """run_diagnostics IS handed a router (workspace.diagnostic_router()) but
    used to pass the bare
    `ollama_report` function reference straight to `_info_probe`, so it ran
    with NO arguments and quietly built its own throwaway `ModelRouter()` --
    dropping any non-default URL/token the caller's router carried. Pin the
    fix directly: a router whose `local_endpoint.origin` differs from the
    process default must be the URL diagnostics actually probes."""
    from frisket.operability import diagnostics
    from frisket.ai.llm import ModelRouter
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    seen: list[str] = []

    def probe(origin: str, **_kwargs: Any) -> dict[str, Any]:
        seen.append(origin)
        return {
            "reachable": False,
            "models": [],
            "detail": "offline",
            "protocol": "unknown",
            "auth_status": "unknown",
        }

    from frisket.server import provider_config

    monkeypatch.setattr(provider_config, "ollama_reachable", probe)
    router = ModelRouter(
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="desk",
                display_name="Desk",
                origin="http://10.9.9.9:11434",
                source="local_file",
            ),
        )
    )
    report = diagnostics.run_diagnostics(router=router)
    endpoint = report["info"]["local_model_endpoints"]["endpoints"][0]
    assert endpoint["endpoint_id"] == "desk"
    assert endpoint["origin"] == "http://10.9.9.9:11434"
    assert seen == ["http://10.9.9.9:11434"]


def test_local_endpoint_report_sends_the_router_configured_bearer(monkeypatch):
    """The probe must actually carry the router's inference token, not just
    its URL -- otherwise a token-fronted endpoint reports "unreachable" via
    diagnostics even though the adapter itself would authenticate fine."""
    from frisket.operability import diagnostics
    from frisket.ai.llm import ModelRouter
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig
    from frisket.server import provider_config

    seen: list[tuple[str, str | None]] = []

    def probe(origin: str, **kwargs: Any) -> dict[str, Any]:
        seen.append((origin, kwargs.get("token")))
        return {
            "reachable": False,
            "models": [],
            "detail": "offline",
            "protocol": "unknown",
            "auth_status": "unknown",
        }

    monkeypatch.setattr(provider_config, "ollama_reachable", probe)

    router = ModelRouter(
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="desk",
                display_name="Desk",
                origin="http://127.0.0.1:1",
                inference_token="secret-diag-token",
                source="local_file",
            ),
        )
    )
    diagnostics.local_model_endpoints_report(router=router, timeout=0.2)
    assert seen == [("http://127.0.0.1:1", "secret-diag-token")]


def test_doctor_and_diagnose_share_the_probe_module(monkeypatch):
    """``frisket doctor`` must call ``frisket.diagnostics`` instead of
    reimplementing probes inline. Otherwise doctor and Diagnose can drift and
    regress to exposing raw stderr-grade strings."""
    import frisket.cli as cli
    from frisket.operability import diagnostics

    monkeypatch.setattr(
        diagnostics, "provider_report", lambda *a, **k: {"summary": "SENTINEL-42"}
    )
    monkeypatch.setattr(
        diagnostics,
        "local_model_endpoints_report",
        lambda *a, **k: {"configured": 0, "reachable": 0, "endpoints": []},
    )

    import io
    import contextlib

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cli.doctor_cmd()
    assert rc == 0
    assert "SENTINEL-42" in out.getvalue()

    # the JSON route reflects the same monkeypatched module state
    report = diagnostics.run_diagnostics()
    assert report["info"]["model_providers"]["summary"] == "SENTINEL-42"
