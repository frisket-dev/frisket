"""Route-resolution: adapter env-decoupling + the execute-seam one-ledger wiring.

With a route binding in ``OpContext.extras`` the transcription adapters take
their connection material from the route's target deref
(``ConnectionConfig``), never from ``FRISKET_MODELS_URL``/``TOKEN``; without
one (previews / non-routed calls) the engine-roster cutover
routes dispatch through the EPHEMERAL binding helpers — the marked
pre-route env fallbacks are deleted, so adapters always receive a
``ConnectionConfig``.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import asyncio
import wave

import httpx
import pytest

from frisket.engine.store.execution_routes import RouteRow
from frisket.execution.credential_use import CredentialOwner, CredentialUseContext
from frisket.execution.provider import ConnectionConfig
from frisket.execution.resolver import CandidateBinding, RouteRowFacts
from frisket.execution.attempt import (
    ATTEMPT_EXTRA,
    AttemptCommitment,
    RoutedAdmission,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY
from frisket.execution.targets import ExecutionTarget
from frisket.ops._sidecar import sidecar_post
from frisket.ops.base import OpContext
from frisket.sdk.ops.transcription.sidecar import sidecar_transcribe_timeout


def _attempt_extras(route: RouteRow, binding: CandidateBinding, **extra) -> dict:
    """An adapter sees the route and its binding as one value —
    the attempt in ``extras[ATTEMPT_EXTRA]``. The four route-binding extras
    keys are gone, and with them the route-without-binding state the old
    ``require_route_binding`` fence existed to catch: a ``RoutedAdmission``
    carries both or does not exist."""
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


def _credential_context(cost_posture: str = "operator_borne") -> CredentialUseContext:
    if cost_posture == "platform_metered":
        owner = CredentialOwner.deployment("test-hosted-deployment")
        return CredentialUseContext(
            cost_posture=cost_posture,
            consented_owner=owner,
            selected_owner=owner,
            deployment_owner=owner,
        )
    return CredentialUseContext.open()


def _route(**overrides) -> RouteRow:
    base = dict(
        id="route_TESTROUTE0000000000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="whisper-turbo",
        options={},
        target_snapshot=_snapshot("models-gateway", "frisket.transcription.v1"),
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


def _snapshot(
    target_id: str,
    transport: str,
    *,
    run_scoped: bool = False,
) -> dict:
    return {
        "target_id": target_id,
        "capability": "transcribe",
        "transport": transport,
        "run_scoped": run_scoped,
    }


def _local_binding() -> CandidateBinding:
    """A deref'd binding for the local target (F6: routed extras are
    all-or-nothing — a route always travels with its binding)."""
    return CandidateBinding(
        facts=RouteRowFacts(
            target_id="local",
            engine="faster_whisper",
            operator="self",
            egress_class="none",
            region=None,
            credential_source="local",
            cost_posture="operator_borne",
        ),
        connection=ConnectionConfig(),
        target=ExecutionTarget(id="local", operator="self", egress_class="none"),
    )


def _gateway_binding(connection: ConnectionConfig) -> CandidateBinding:
    return CandidateBinding(
        facts=RouteRowFacts(
            target_id="models-gateway",
            engine="faster_whisper",
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


# ---------------------------------------------------------------------------
# sidecar_post: route-bound connection vs the pre-route env fallback.
# ---------------------------------------------------------------------------


def test_sidecar_post_uses_route_connection_not_env(monkeypatch):
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    captured: list[httpx.Request] = []
    ctx = OpContext(http=_capture_client(captured, {"ok": True}))
    connection = ConnectionConfig(base_url="http://route-gw:9000", token="route-token")
    body = asyncio.run(
        sidecar_post(ctx, "/ocr", data={"engine": "x"}, connection=connection)
    )
    assert body == {"ok": True}
    (request,) = captured
    assert str(request.url) == "http://route-gw:9000/ocr"
    assert request.headers["Authorization"] == "Bearer route-token"


def test_sidecar_post_uses_route_connection_timeout_when_caller_omits_it():
    captured = {}

    class FakeHttp:
        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return httpx.Response(200, json={"ok": True})

    connection = ConnectionConfig(
        base_url="http://route-gw:9000",
        token="route-token",
        timeout_seconds=47.0,
        connect_timeout_seconds=3.0,
    )
    body = asyncio.run(
        sidecar_post(
            OpContext(http=FakeHttp()),
            "/ner",
            json={"texts": ["hello"]},
            connection=connection,
        )
    )

    assert body == {"ok": True}
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 3.0
    assert timeout.read == 47.0
    assert timeout.write == 47.0
    assert timeout.pool == 47.0


def test_sidecar_post_preserves_explicit_caller_timeout():
    captured = {}

    class FakeHttp:
        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return httpx.Response(200, json={"ok": True})

    explicit = httpx.Timeout(connect=1.0, read=2.0, write=3.0, pool=4.0)
    body = asyncio.run(
        sidecar_post(
            OpContext(http=FakeHttp()),
            "/ocr",
            data={"engine": "dots.mocr"},
            timeout=explicit,
            connection=ConnectionConfig(
                base_url="http://route-gw:9000",
                token="route-token",
                timeout_seconds=47.0,
                connect_timeout_seconds=3.0,
            ),
        )
    )

    assert body == {"ok": True}
    assert captured["timeout"] is explicit


def test_sidecar_post_follows_modal_result_redirect() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/ocr":
            return httpx.Response(303, headers={"Location": "/modal-result"})
        return httpx.Response(200, json={"ok": True})

    async def request() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await sidecar_post(
                OpContext(http=client),
                "/ocr",
                data={"engine": "dots.mocr"},
                connection=ConnectionConfig(
                    base_url="http://route-gw:9000", token="route-token"
                ),
            )

    assert asyncio.run(request()) == {"ok": True}
    assert [(item.method, item.url.path) for item in captured] == [
        ("POST", "/ocr"),
        ("GET", "/modal-result"),
    ]


def test_sidecar_post_route_connection_missing_material_fails_loudly():
    ctx = OpContext(http=_capture_client([], {}))
    with pytest.raises(RuntimeError, match="route-bound"):
        asyncio.run(
            sidecar_post(ctx, "/ocr", data={}, connection=ConnectionConfig(token="t"))
        )


def test_sidecar_post_unrouted_unconfigured_gateway_refuses_with_remedy(monkeypatch):
    # There is no environment fallback — the unrouted path derefs the models-gateway
    # target ephemerally, and an unconfigured gateway refuses with the
    # target's activation remedy (which names the env knobs to set).
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    ctx = OpContext(http=_capture_client([], {}))
    with pytest.raises(RuntimeError, match="FRISKET_MODELS_URL"):
        asyncio.run(sidecar_post(ctx, "/ocr", data={"engine": "x"}))


def test_sidecar_post_unrouted_configured_gateway_binds_ephemerally(monkeypatch):
    # A configured gateway serves the unrouted call through the SAME
    # candidate-binding deref the routed path uses — env is read only at
    # the definitions home.
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://env-gw:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "env-token")
    captured: list[httpx.Request] = []
    ctx = OpContext(http=_capture_client(captured, {"ok": True}))
    body = asyncio.run(sidecar_post(ctx, "/ocr", data={"engine": "x"}))
    assert body == {"ok": True}
    (request,) = captured
    assert str(request.url) == "http://env-gw:8500/ocr"
    assert request.headers["Authorization"] == "Bearer env-token"


def test_sidecar_timeout_comes_only_from_the_connection(monkeypatch):
    # The environment setting is read once at the definitions home and rides
    # the ConnectionConfig; this module never consults env — a poisoned
    # value proves it on both the routed and the timeout-less shapes.
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS", "not-a-number")
    timeout = sidecar_transcribe_timeout(
        ConnectionConfig(
            base_url="http://gw",
            token="t",
            timeout_seconds=120.0,
            connect_timeout_seconds=5.0,
        )
    )
    assert timeout.read == 120.0
    assert timeout.connect == 5.0
    # A connection without a timeout budget gets the code defaults.
    bare = sidecar_transcribe_timeout(ConnectionConfig(base_url="http://gw", token="t"))
    assert bare.read == 3600.0
    # At the definitions home itself the poisoned knob fails LOUD on deref,
    # naming the env var — an operator typo must never silently become the
    # default one-hour budget.
    from frisket.execution.definitions import StaticExecutionTargetProvider

    # Unparseable, non-positive, and non-finite are the same operator-typo
    # trap (NaN fails every comparison, so it needs its own guard).
    for poisoned in ("not-a-number", "0", "-5", "nan", "inf", "-inf"):
        provider = StaticExecutionTargetProvider(
            env={
                "FRISKET_MODELS_URL": "http://gw",
                "FRISKET_MODELS_TOKEN": "t",
                "FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS": poisoned,
            }
        )
        with pytest.raises(
            ValueError, match="FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS"
        ):
            provider.connection("models-gateway")


def test_route_without_binding_is_unconstructible(tmp_path, monkeypatch):
    """B4: the state the old ``require_route_binding`` fence existed to catch
    — a route in scope WITHOUT its binding — is no longer representable, so
    the fence has nothing left to guard.

    Two halves. (a) ``RoutedAdmission`` requires the binding: there is no way
    to build one carrying a route alone. (b) Type, not shape: a hand-built
    dict in the attempt slot is NOT an attempt, so no route is in scope at
    all and the invocation takes the honest unrouted path rather than
    dispatching under a half-built route."""
    from frisket.execution.attempt import routed_admission_in_scope

    with pytest.raises(TypeError):
        RoutedAdmission(  # type: ignore[call-arg]
            head_route_id="route_x",
            head_promise_set_id="pset_x",
            route=_route(),
            promise_set=None,
            evaluation=None,
            admitted_by_consent_id=None,
        )

    forged = {ATTEMPT_EXTRA: {"route": _route(), "binding": None}}
    assert routed_admission_in_scope(forged) is None


def test_run_sidecar_uses_route_binding_end_to_end(monkeypatch):
    monkeypatch.delenv("FRISKET_MODELS_URL", raising=False)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
    captured: list[httpx.Request] = []
    connection = ConnectionConfig(
        base_url="http://route-gw:9000", token="route-token", timeout_seconds=90.0
    )
    ctx = OpContext(
        http=_capture_client(
            captured,
            {
                "contract_version": "frisket.transcription.v1",
                "results": [
                    {
                        "engine": "whisper-turbo",
                        "text": "hi",
                        "segments": [],
                        "language": "en",
                        "duration": 0.1,
                        "model_ids": ["dropbox-dash/faster-whisper-large-v3-turbo"],
                        "revision": "pinned",
                        "device": "cpu",
                        "dtype": "int8",
                        "timings": {"inference_seconds": 0.1},
                        "warnings": [],
                        "accepted_options": {},
                    }
                ],
            },
        ),
        extras=_attempt_extras(_route(), _gateway_binding(connection)),
    )
    out = asyncio.run(
        transcribe_engines.TranscriptionV1Adapter().transcribe(
            "whisper-turbo", __file__, {}, ctx
        )
    )
    assert out["text"] == "hi"
    (request,) = captured
    assert str(request.url) == "http://route-gw:9000/v1/transcribe"
    assert request.headers["Authorization"] == "Bearer route-token"


# ---------------------------------------------------------------------------
# execute(): the one-ledger seam binds facts iff a route is in scope.
# ---------------------------------------------------------------------------


def _silent_wav(path) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return str(path)


def test_execute_binds_facts_to_route_in_scope(tmp_path, monkeypatch):
    async def fake_run_engine(self, path, spec, *, should_cancel=None):
        return {"text": "hi", "segments": [], "language": None, "cost": 0.0}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_run_engine
    )
    route = _route(
        engine="faster_whisper",
        target_snapshot=_snapshot("local", "local"),
    )
    wav = _silent_wav(tmp_path / "a.wav")
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "faster_whisper",
            wav,
            {"engine": "faster_whisper"},
            OpContext(extras=_attempt_extras(route, _local_binding())),
            transport="local",
        )
    )
    (call,) = result.model_calls
    assert call["provider"] == "local"
    assert call["credential_source"] == route.credential_source
    assert call["cost_source"] == "free_local"
    observation = call[ROUTE_OBSERVATION_KEY]
    assert observation["route_id"] == route.id


def test_execute_without_route_keeps_factory_facts(tmp_path, monkeypatch):
    async def fake_run_engine(self, path, spec, *, should_cancel=None):
        return {"text": "hi", "segments": [], "language": None, "cost": 0.0}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_run_engine
    )
    wav = _silent_wav(tmp_path / "a.wav")
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "faster_whisper",
            wav,
            {"engine": "faster_whisper"},
            OpContext(),
            transport="local",
        )
    )
    (call,) = result.model_calls
    assert ROUTE_OBSERVATION_KEY not in call
    assert call["provider"] == "local"
    assert call["cost_source"] == "free_local"


def test_execute_asserts_observation_present_under_route(tmp_path, monkeypatch):
    """A fact built under a route without the
    observation payload is a defect and fails loudly."""

    async def fake_run_engine(self, path, spec, *, should_cancel=None):
        return {"text": "hi", "segments": [], "language": None, "cost": 0.0}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_run_engine
    )

    def unbound(route, call, out):
        stripped = dict(call)
        stripped.pop(ROUTE_OBSERVATION_KEY, None)
        return stripped

    import frisket.sdk.ops.transcribe_engines as transcribe_module

    monkeypatch.setattr(transcribe_module, "bind_fact_to_route", unbound)
    wav = _silent_wav(tmp_path / "a.wav")
    with pytest.raises(RuntimeError, match="epoch invariant"):
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "faster_whisper",
                wav,
                {"engine": "faster_whisper"},
                OpContext(
                    extras=_attempt_extras(
                        _route(
                            engine="faster_whisper",
                            target_snapshot=_snapshot("local", "local"),
                        ),
                        _local_binding(),
                    )
                ),
                transport="local",
            )
        )


# ---------------------------------------------------------------------------
# The ZERO-model-call receipt labels its provider from the ROUTE, not from
# the static per-engine map.
# ---------------------------------------------------------------------------


def test_zero_call_receipt_does_not_invent_a_provider():
    # The selected venue belongs to route evidence, not actual provider use.
    from frisket.engine.executor.action_support import _routed_call_provider_use

    uses = _routed_call_provider_use([], capability="transcribe")
    assert not any(use.get("provider") for use in uses)
    assert all(use.get("request_count", 0) == 0 for use in uses)


def test_provider_label_refuses_a_downstream_target_without_an_operator():
    from frisket.execution.runtime_binding import _provider_for_target

    with pytest.raises(ValueError, match="route operator"):
        _provider_for_target("venus:1", "")


def test_provider_label_uses_the_pinned_operator_for_a_downstream_target():
    from frisket.execution.runtime_binding import _provider_for_target

    assert _provider_for_target("venus:1", "venus-operator") == "venus-operator"


def test_provider_label_is_target_keyed_not_transport_keyed():
    from frisket.execution.runtime_binding import _provider_for_target

    assert _provider_for_target("local", "self") != _provider_for_target(
        "local-onnx", "self"
    )
