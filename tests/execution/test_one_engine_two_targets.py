"""One Parakeet identity served locally and through the models gateway."""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines
import pytest

from frisket.actions.media import TranscribeParams
from frisket.engine.store.execution_routes import RouteRow, provenance_hash
from frisket.execution.definitions import (
    StaticExecutionTargetProvider,
    build_static_targets,
)
from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import (
    Refusal,
    Resolution,
    ResolutionRequest,
    preferred_static_choice,
    resolve,
)
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route

OPEN = CompositionFacts()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in (
        "FRISKET_MODELS_URL",
        "FRISKET_MODELS_TOKEN",
        "HF_HUB_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "HF_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))


def _activate_both(monkeypatch, tmp_path) -> None:
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    monkeypatch.setenv("FRISKET_MODELS_URL", "https://models.example.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-token")
    cache = tmp_path / "hub"
    for entry in (
        artifact_manifest.parakeet_model_artifact(),
        artifact_manifest.parakeet_vad_artifact(),
    ):
        assert entry is not None and entry.hf_snapshot is not None
        snap = entry.hf_snapshot
        base = (
            cache
            / f"models--{snap.repo_id.replace('/', '--')}"
            / "snapshots"
            / snap.revision
        )
        for name in snap.files:
            target = base / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"pinned")
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))


def _resolve(engine: str, options: dict | None = None) -> Resolution | Refusal:
    return resolve(
        ResolutionRequest(engine=engine, options=options or {}),
        StaticExecutionTargetProvider(),
        OPEN,
    )


def test_same_engine_resolves_local_or_gateway_from_options(monkeypatch, tmp_path):
    _activate_both(monkeypatch, tmp_path)

    local = _resolve("parakeet-tdt")
    hosted = _resolve("parakeet-tdt", {"diarize": True})

    assert isinstance(local, Resolution) and isinstance(hosted, Resolution)
    assert local.facts.engine == hosted.facts.engine == "parakeet-tdt"
    assert local.target.id == "local-onnx"
    assert hosted.target.id == "models-gateway"
    assert local.support.transport == "local"
    assert hosted.support.transport == "frisket.transcription.v1"
    assert local.support.run_scoped is True
    assert hosted.support.run_scoped is False
    assert local.support.options.diarization_mode == "none"
    assert hosted.support.options.diarization_mode == "optional"


def test_diarize_authoring_is_valid_on_the_union():
    params = TranscribeParams.model_validate(
        {
            "source": "media",
            "engine": "parakeet-tdt",
            "diarize": True,
        }
    )
    assert params.diarize is True


def test_diarize_on_local_only_roster_refuses_never_drops():
    local_only = tuple(
        target for target in build_static_targets() if target.id != "models-gateway"
    )
    choice = preferred_static_choice("parakeet-tdt", {"diarize": True}, local_only)
    assert isinstance(choice, Refusal)
    assert choice.family == "no_capable_target"
    assert "diarization" in choice.remedy


def test_diarize_with_dead_gateway_never_substitutes_local(monkeypatch, tmp_path):
    _activate_both(monkeypatch, tmp_path)
    monkeypatch.delenv("FRISKET_MODELS_TOKEN")

    result = _resolve("parakeet-tdt", {"diarize": True})

    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "models-gateway"
    assert "FRISKET_MODELS_TOKEN" in result.remedy


def _route_for(target_id: str, transport: str) -> RouteRow:
    return RouteRow(
        id=f"route_{target_id.replace('-', '_')}",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="parakeet-tdt",
        options={},
        target_snapshot={
            "target_id": target_id,
            "capability": "transcribe",
            "transport": transport,
            "run_scoped": transport == "local",
        },
        route_fact_hash="hash",
        operator="self",
        egress_class="none" if transport == "local" else "operator_lan",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
        created_at="2026-07-24T00:00:00+00:00",
    )


def _attempt_ctx(route: RouteRow):
    from frisket.execution.attempt import (
        ATTEMPT_EXTRA,
        AttemptCommitment,
        RoutedAdmission,
    )
    from frisket.execution.promise_compiler import OperatorBorneZeroCost
    from frisket.execution.provider import ConnectionConfig
    from frisket.execution.resolver import CandidateBinding, RouteRowFacts
    from frisket.execution.targets import ExecutionTarget
    from frisket.ops.base import OpContext

    facts = RouteRowFacts(
        target_id=route.target_snapshot["target_id"],
        engine=route.engine,
        operator=route.operator,
        egress_class=route.egress_class,
        region=route.region,
        credential_source=route.credential_source,
        cost_posture=route.cost_posture,
    )
    binding = CandidateBinding(
        facts=facts,
        connection=ConnectionConfig(base_url="http://target", token=None),
        target=ExecutionTarget(
            id=facts.target_id,
            operator=route.operator,
            egress_class=route.egress_class,
        ),
    )
    return OpContext(
        extras={
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
            )
        }
    )


def test_routes_preserve_build_provenance_and_dispatch():
    local_route = _route_for("local-onnx", "local")
    gateway_route = _route_for("models-gateway", "frisket.transcription.v1")
    call = {"engine": "parakeet-tdt", "units": {}}
    local = bind_fact_to_route(local_route, dict(call), {"revision": "onnx-int8"})
    hosted = bind_fact_to_route(gateway_route, dict(call), {"revision": "gpu"})
    local_observed = local[ROUTE_OBSERVATION_KEY]["observed"]
    hosted_observed = hosted[ROUTE_OBSERVATION_KEY]["observed"]

    assert provenance_hash(local_observed) != provenance_hash(hosted_observed)
    spec = {"engine": "parakeet-tdt"}
    assert (
        transcribe_engines.transcription_transport(
            "parakeet-tdt", spec, _attempt_ctx(local_route)
        )
        == "local"
    )
    assert (
        transcribe_engines.transcription_transport(
            "parakeet-tdt", spec, _attempt_ctx(gateway_route)
        )
        == "frisket.transcription.v1"
    )


def test_run_scope_remains_local_only():
    assert (
        transcribe_engines.transcription_max_row_concurrency({"engine": "parakeet-tdt"})
        == 1
    )
    assert (
        transcribe_engines.transcription_max_row_concurrency(
            {"engine": "parakeet-tdt", "diarize": True}
        )
        is None
    )
