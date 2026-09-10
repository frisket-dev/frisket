"""The ``to_markdown`` sidecar dispatch takes its connection
material from the route binding, never from a fresh ephemeral dereference of
ambient env.

``_convert_sidecar`` once called
``sidecar_post`` with no ``connection=``, so a routed docling/chandra call
re-derived a FRESH ephemeral gateway deref at dispatch time instead of using
the one it was ADMITTED against. If ambient config (env, or whatever the
gateway target's definition reads) moved between admission and dispatch, the
document + bearer token could go to a different gateway than the one the
consent named — or fail despite the admitted gateway being perfectly live.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from frisket.engine.store.execution_routes import RouteRow
from frisket.execution.provider import ConnectionConfig
from frisket.execution.resolver import CandidateBinding, RouteRowFacts
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    RoutedAdmission,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.targets import ExecutionTarget
from frisket.ops.base import OpContext, RecipeInvocationHalt
from tests.document_conversion_helpers import bound_document_converter


def _attempt_extras(route: RouteRow, binding: CandidateBinding, **extra) -> dict:
    """An adapter sees the route and its binding as one value —
    the attempt in ``extras[ATTEMPT_EXTRA]``. The shape is capability-neutral."""
    return {
        ATTEMPT_EXTRA: AttemptCommitment(
            attempt_id="attempt_TESTATTEMPT000000000000",
            run_id=1,
            seq=0,
            identity="identity",
            scope=(1,),
            admission=RoutedAdmission(
                head_route_id=route.id,
                head_promise_set_id=route.promise_set_id,
                route=route,
                promise_set=None,
                binding=binding,
                evaluation=None,
                admitted_by_consent_id=None,
            ),
            cost_basis=OperatorBorneZeroCost(),
            price_card_version=None,
        ),
        **extra,
    }


def _route(**overrides) -> RouteRow:
    base = dict(
        id="route_TESTROUTE0000000000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="docling",
        options={},
        target_snapshot={
            "target_id": "models-gateway",
            "capability": "document.convert",
            "transport": "sidecar.convert",
            "run_scoped": False,
        },
        route_fact_hash="hash",
        operator="self",
        egress_class="operator_lan",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
        created_at="2026-07-24T00:00:00+00:00",
    )
    base.update(overrides)
    return RouteRow(**base)


def _gateway_binding(connection: ConnectionConfig) -> CandidateBinding:
    return CandidateBinding(
        facts=RouteRowFacts(
            target_id="models-gateway",
            engine="docling",
            operator="self",
            egress_class="operator_lan",
            region=None,
            credential_source="local",
            cost_posture="operator_borne",
        ),
        connection=connection,
        target=ExecutionTarget(
            id="models-gateway", operator="self", egress_class="operator_lan"
        ),
    )


def _capture_client(captured: list[httpx.Request], body: dict) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_convert_sidecar_uses_route_binding_not_ambient_env(monkeypatch):
    """The A-vs-B proof: admission bound gateway A; env is then changed to a
    DIFFERENT, equally-live gateway B before dispatch. Existing tests hold
    the env constant (or unset) and so cannot see a fresh-ephemeral-deref
    regression that happens to land on an unconfigured/absent gateway — this
    one proves the admitted connection wins even when B is a real,
    dereffable alternative the bug would happily send the document to."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway-b:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-b-token")
    captured: list[httpx.Request] = []
    connection_a = ConnectionConfig(
        base_url="http://gateway-a:9000", token="gateway-a-token"
    )
    ctx = OpContext(
        http=_capture_client(
            captured, {"documents": [{"markdown": "# From A", "ocr_used": [False]}]}
        ),
        extras=_attempt_extras(_route(), _gateway_binding(connection_a)),
    )
    recipe = bound_document_converter(ctx, engine="docling")
    markdown, ocr_used = asyncio.run(recipe._convert_sidecar("docling", Path(__file__)))
    assert markdown == "# From A"
    assert ocr_used == [False]
    (request,) = captured
    assert str(request.url) == "http://gateway-a:9000/to-markdown"
    assert request.headers["Authorization"] == "Bearer gateway-a-token"


def test_convert_sidecar_without_admission_refuses_before_http(monkeypatch):
    """Even valid ambient credentials cannot authorize an unadmitted upload."""
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://env-gw:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "env-token")
    captured: list[httpx.Request] = []
    ctx = OpContext(
        http=_capture_client(
            captured, {"documents": [{"markdown": "# From env", "ocr_used": []}]}
        )
    )
    converter = bound_document_converter(ctx, engine="docling")
    with pytest.raises(RecipeInvocationHalt, match="admitted") as refusal:
        asyncio.run(converter._convert_sidecar("docling", Path(__file__)))
    assert refusal.value.code == "promise_violation"
    assert captured == []
