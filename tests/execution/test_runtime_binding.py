"""Run-time route binding and one-ledger derivation.

- the replacement for ``load_route_binding``: the attempt authority
  derefs the head route's binding by snapshot id ONLY (never a re-choice),
  refuses ``no_live_target`` when the deref fails, and hands the pair down as
  ONE ``AttemptCommitment`` in ``extras``. B3/B4: there is no
  route-without-binding state left to fence, because ``RoutedAdmission``
  carries both or does not exist.
- ``bind_fact_to_route``: for every transcription path
  (local / local-onnx / v1 / remote) the promised
  receipt fields — provider grouping, provider kind, credential source,
  cost_source — derive from the route ROW + snapshot, never env/literals;
  the receipt-diff acceptance checks the bound fact against the route
  row's own columns. Observed-only facts (revision/device/dtype) ride the
  observation payload; per-call timings do not (they are never epoch
  identity).

The companion pin on what those dispatch files may read out of ``os.environ``
is a source check, so it lives in ``scripts/ci/lint_placement_env_reads.py``.
"""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

import pytest
from helpers import replace_test_source_cell

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteRow, RouteStore
from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.provider import (
    CompositionFacts,
    ConnectionConfig,
    ExecutionComposition,
)
from frisket.execution.resolver import CandidateBinding
from frisket.execution.runtime_binding import (
    ROUTE_OBSERVATION_KEY,
    bind_fact_to_route,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import typed_queued_map_spec
from frisket.execution.targets import ExecutionTarget

PROMISES = [
    {
        "field": "operator",
        "op": "eq",
        "value": "self",
        "basis": None,
        "order_ref": None,
        "audience": "system_promise",
    },
    {
        "field": "egress_class",
        "op": "eq",
        "value": "none",
        "basis": None,
        "order_ref": None,
        "audience": "user_claim",
    },
]


class _CallFact(dict):
    def as_dict(self) -> dict:
        return dict(self)


def _model_call_for(*args, **kwargs) -> _CallFact:
    return _CallFact(transcribe_engines.transcription_model_calls(*args, **kwargs)[0])


def _snapshot(
    target_id: str,
    transport: str,
    *,
    capability: str = "transcribe",
    run_scoped: bool = False,
) -> dict:
    return {
        "target_id": target_id,
        "capability": capability,
        "transport": transport,
        "run_scoped": run_scoped,
    }


def _route(**overrides) -> RouteRow:
    base = dict(
        id="route_TESTROUTE0000000000000000",
        subject_kind="run",
        subject_id="1",
        seq=1,
        predecessor_id=None,
        promise_set_id="pset_TEST",
        engine="faster_whisper",
        options={},
        target_snapshot=_snapshot("local", "local"),
        route_fact_hash="hash",
        operator="self",
        egress_class="none",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
        created_at="2026-07-24T00:00:00+00:00",
    )
    base.update(overrides)
    return RouteRow(**base)


def _observation(bound: dict) -> dict:
    payload = bound.get(ROUTE_OBSERVATION_KEY)
    assert payload, "route-bound fact must carry the observation payload"
    return payload


# ---------------------------------------------------------------------------
# One-ledger derivation: receipt-diff against the route row, per path.
# ---------------------------------------------------------------------------


def _assert_promised_fields_match_route(bound: dict, route: RouteRow) -> None:
    """Settlement-visible promised fields come from the route row."""
    assert bound["credential_source"] == route.credential_source
    observation = _observation(bound)
    assert observation["route_id"] == route.id
    assert observation["observed"]["target_id"] == route.target_snapshot["target_id"]
    assert observation["observed"]["credential_source"] == bound["credential_source"]


def test_local_fact_derives_from_route_row():
    route = _route()
    call = _model_call_for(
        "faster_whisper", "/nonexistent.wav", {"model_size": "base"}, {}
    ).as_dict()
    bound = bind_fact_to_route(route, call, {})
    assert bound["provider"] == "local"
    assert bound["provider_kind"] == "local_process"
    assert bound["cost_source"] == "free_local"
    _assert_promised_fields_match_route(bound, route)


def test_snapshot_capability_is_fact_provenance_authority():
    transcribe = _route()
    ocr = _route(
        engine="rapidocr",
        target_snapshot=_snapshot(
            "local",
            "local",
            capability="ocr",
        ),
    )
    transcribe_bound = bind_fact_to_route(
        transcribe,
        {"capability": "ocr", "engine": "faster_whisper"},
        {},
    )
    ocr_bound = bind_fact_to_route(
        ocr,
        {"capability": "transcribe", "engine": "rapidocr"},
        {},
    )

    assert _observation(transcribe_bound)["observed"]["provenance"] == (
        "frisket.transcribe_binding.v1"
    )
    assert _observation(ocr_bound)["observed"]["provenance"] == (
        "frisket.ocr_binding.v1"
    )


def test_local_onnx_fact_derives_provider_from_snapshot():
    route = _route(
        engine="parakeet-tdt",
        target_snapshot=_snapshot("local-onnx", "local", run_scoped=True),
    )
    call = _model_call_for("parakeet-tdt", "/nonexistent.wav", {}, {}).as_dict()
    bound = bind_fact_to_route(route, call, {})
    assert bound["provider"] == "local-onnx"
    assert bound["provider_kind"] == "local_process"
    assert bound["cost_source"] == "free_local"
    _assert_promised_fields_match_route(bound, route)


def test_sidecar_v1_fact_derives_from_route_row():
    route = _route(
        engine="faster_whisper",
        target_snapshot=_snapshot("models-gateway", "frisket.transcription.v1"),
        egress_class="operator_lan",
    )
    out = {"duration": 2.0}
    # The hyphenated name survives only as the gateway WIRE name; the
    # fact's engine field carries what dispatch stamped.
    call = _CallFact(
        transcribe_engines.TranscriptionV1Adapter().model_calls(
            "faster-whisper", "/nonexistent.wav", {}, out
        )[0]
    )
    bound = bind_fact_to_route(route, call.as_dict(), out)
    assert bound["provider"] == "frisket-sidecar"
    assert bound["provider_kind"] == "local_http"
    assert bound["cost_source"] == "free_local"
    _assert_promised_fields_match_route(bound, route)


def test_sidecar_v1_observed_only_facts_ride_the_observation():
    route = _route(
        engine="moss",
        target_snapshot=_snapshot("models-gateway", "frisket.transcription.v1"),
        egress_class="operator_lan",
    )
    out = {
        "revision": "2026.07-a",
        "device": "cuda",
        "dtype": "float16",
        "timings": {"decode_s": 1.25},
        "model_ids": ["moss-large"],
    }
    call = _model_call_for("moss", "/nonexistent.wav", {}, out).as_dict()
    bound = bind_fact_to_route(route, call, out)
    observed = _observation(bound)["observed"]
    assert observed["revision"] == "2026.07-a"
    assert observed["device"] == "cuda"
    assert observed["dtype"] == "float16"
    # Timings vary per call and are NEVER epoch identity — they stay on the
    # fact's units, out of the provenance payload.
    assert not any(key.startswith("timing") for key in observed)
    assert bound["units"]["timing_decode_s"] == 1.25
    _assert_promised_fields_match_route(bound, route)


def test_downstream_target_fact_groups_by_its_pinned_operator():
    route = _route(
        engine="parakeet-tdt",
        target_snapshot=_snapshot("synthetic-shared", "remote"),
        operator="synthetic-operator",
        egress_class="frisket_shared",
        credential_source="platform_key",
        cost_posture="platform_metered",
    )
    bound = bind_fact_to_route(
        route,
        {
            "engine": "parakeet-tdt",
            "provider_cost_usd": 0.02,
            "credential_source": "platform_key",
        },
        {},
    )

    assert bound["provider"] == "synthetic-operator"
    assert bound["provider_kind"] == "platform_api"
    assert bound["cost_source"] == "pricing_data"


def test_remote_fact_derives_provider_and_pricing_from_route():
    route = _route(
        engine="openai/whisper-1",
        target_snapshot=_snapshot("remote-api:openai", "remote"),
        operator="openai",
        egress_class="third_party_api",
    )
    out = {"cost": 0.006, "usage": {}, "credential_source": "project_key"}
    call = _model_call_for("openai/whisper-1", "/nonexistent.wav", {}, out).as_dict()
    bound = bind_fact_to_route(route, call, out)
    assert bound["provider"] == "openai"
    assert bound["provider_kind"] == "platform_api"
    assert bound["cost_source"] == "pricing_data"
    assert bound["credential_source"] == "project_key"


def test_remote_fact_without_price_is_unknown():
    route = _route(
        engine="openai/gpt-4o-transcribe",
        target_snapshot=_snapshot("remote-api:openai", "remote"),
    )
    out = {"cost": None, "usage": {}, "credential_source": "local"}
    call = _model_call_for(
        "openai/gpt-4o-transcribe", "/nonexistent.wav", {}, out
    ).as_dict()
    bound = bind_fact_to_route(route, call, out)
    assert bound["cost_source"] == "unknown"


def test_org_key_posture_maps_to_provider_billed():
    route = _route(
        engine="openai/whisper-1",
        target_snapshot=_snapshot("remote-api:openai", "remote"),
        cost_posture="org_key",
        credential_source="org_byok",
    )
    out = {"cost": 0.006, "usage": {}, "credential_source": "org_byok"}
    call = _model_call_for("openai/whisper-1", "/nonexistent.wav", {}, out).as_dict()
    bound = bind_fact_to_route(route, call, out)
    assert bound["cost_source"] == "provider_billed"


def test_unknown_transport_fails_loudly():
    route = _route(target_snapshot=_snapshot("local", "carrier-pigeon"))
    with pytest.raises(ValueError, match="transport"):
        bind_fact_to_route(route, {"engine": "faster_whisper"}, {})


# ---------------------------------------------------------------------------
# The attempt's binding seam (B2/B3/B4): ONE head read, ONE deref, ONE key.
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    p = Project.create(tmp_path / "t.frisket", name="t")
    yield p
    p.close()


class _OneTargetProvider:
    """A minimal ExecutionTargetProvider: one gateway target, injectable
    liveness answer."""

    def __init__(self, connection: ConnectionConfig | None):
        self._connection = connection
        self.probes: list[str] = []

    def targets(self):
        from frisket.execution.definitions import build_static_targets

        gateway = next(
            target for target in build_static_targets() if target.id == "models-gateway"
        )
        return (
            # egress_class matches the persisted promise below, so the
            # admission's own evaluate_set is SATISFIED and these tests are
            # about the binding seam rather than about a violation.
            ExecutionTarget(
                id="models-gateway",
                operator="self",
                egress_class="none",
                engines=gateway.engines,
            ),
        )

    def connection(self, target_id: str):
        self.probes.append(target_id)
        return self._connection


def _composition(provider) -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(),
        provider=provider,
        credential_use_context=CredentialUseContext.open(),
    )


#: The spec these admissions are asked about. Its canonical identity is what
#: the consent row below is recorded against, so the admission takes the
#: exact-match branch rather than the coverage envelope.
ADMIT_SPEC = {
    "action_kind": "media.transcribe",
    "sheet_id": 1,
    "params": {"source": "media", "engine": "moss"},
}


def _persist_route(project, run_id: int, *, target_id="models-gateway", **facts):
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.execution.resolve_for_action import action_identity_hash

    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(promises=PROMISES, predecessor_id=None)
    store.record_consent(
        action_identity_hash=action_identity_hash(ADMIT_SPEC),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    return store.append_route(
        promise_set_id=promise_set.id,
        engine=facts.pop("engine", "whisper-turbo"),
        options={},
        target_snapshot=_snapshot(target_id, "frisket.transcription.v1"),
        operator=facts.pop("operator", "self"),
        egress_class=facts.pop("egress_class", "none"),
        region=facts.pop("region", None),
        credential_source=facts.pop("credential_source", "local"),
        cost_posture=facts.pop("cost_posture", "operator_borne"),
        predecessor_id=None,
    )


def test_admission_without_route_artifacts_refuses_consent_missing(project):
    """Where ``load_route_binding`` used to return ``{}`` and let dispatch
    proceed unverified, a resolution-consuming recipe with no persisted route
    is now a durable ``consent_missing`` refusal."""
    from frisket.execution.attempt_authority import admit_routed
    from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed

    with pytest.raises(ExecutionRouteVerificationFailed) as exc_info:
        admit_routed(
            project,
            41,
            ADMIT_SPEC,
            composition=_composition(_OneTargetProvider(None)),
        )
    assert exc_info.value.code == "consent_missing"


def test_admission_derefs_the_head_binding_by_snapshot_id_only(project):
    """The deref is a target lookup by SNAPSHOT id, never a re-choice, and it
    happens exactly once inside admission."""
    from frisket.execution.attempt_authority import admit_routed

    route = _persist_route(project, 41)
    connection = ConnectionConfig(base_url="http://gw:9000", token="secret")
    provider = _OneTargetProvider(connection)
    admission = admit_routed(
        project,
        41,
        ADMIT_SPEC,
        composition=_composition(provider),
    )
    assert admission.head_route_id == route.id
    assert admission.head_promise_set_id == route.promise_set_id
    assert isinstance(admission.binding, CandidateBinding)
    assert admission.binding.connection is connection
    assert admission.binding.facts.target_id == "models-gateway"
    assert admission.binding.facts.credential_source == route.credential_source
    assert provider.probes == ["models-gateway"]


def test_admission_refuses_no_live_target_when_the_pin_is_dead(project):
    """F6 survives the move: a persisted route whose pinned target won't
    deref is a hard dispatch refusal — never a route-without-binding value an
    adapter could take an env fallback under."""
    from frisket.execution.attempt_authority import admit_routed
    from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed

    _persist_route(project, 41)
    with pytest.raises(ExecutionRouteVerificationFailed) as exc_info:
        admit_routed(
            project,
            41,
            ADMIT_SPEC,
            composition=_composition(_OneTargetProvider(None)),
        )
    assert exc_info.value.code == "no_live_target"


def test_dispatch_separates_availability_from_confirmed_fact_drift(project):
    from frisket.execution.attempt_authority import admit_routed
    from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed
    from frisket.ops.base import RecipeInvocationHalt

    route = _persist_route(project, 42)

    class Provider:
        def __init__(self, targets, connection):
            self._targets = tuple(targets)
            self._connection = connection

        def targets(self):
            return self._targets

        def connection(self, target_id):
            return self._connection

    connection = ConnectionConfig(
        base_url="http://changed-gateway:9000",
        token="replacement-secret",
        extra={
            "gpu": "cpu",
            "pricing_key": "ambient-definition-drift",
            "unit_rate": "999",
        },
    )
    from frisket.execution.definitions import build_static_targets

    gateway_engines = next(
        target.engines
        for target in build_static_targets()
        if target.id == "models-gateway"
    )
    current = ExecutionTarget(
        id="models-gateway",
        operator="changed-operator",
        egress_class="third_party_api",
        region="eu-west-1",
        engines=gateway_engines,
    )

    for provider in (
        Provider((), connection),
        Provider((current,), None),
    ):
        with pytest.raises(ExecutionRouteVerificationFailed) as exc_info:
            admit_routed(
                project,
                42,
                ADMIT_SPEC,
                composition=_composition(provider),
            )
        assert exc_info.value.code == "no_live_target"

    available = ExecutionTarget(
        id="models-gateway",
        operator="self",
        egress_class="none",
        engines=gateway_engines,
    )
    admission = admit_routed(
        project,
        42,
        ADMIT_SPEC,
        composition=_composition(Provider((available,), connection)),
    )
    assert admission.route.id == route.id
    assert admission.binding.target is available
    assert admission.binding.connection is connection
    assert admission.binding.target.engines == gateway_engines

    # The same exact-ID dereference now observes a definition whose operator
    # and egress rows do not satisfy the confirmed promise set. Exact support
    # remains present; this refusal is about the independently hashed facts.
    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            42,
            ADMIT_SPEC,
            composition=_composition(Provider((current,), connection)),
        )
    assert exc_info.value.code == "promise_violation"
    assert "operator: not_equal" in exc_info.value.detail
    assert "egress_class: not_equal" in exc_info.value.detail
    assert "confirm" in exc_info.value.detail
    assert len(RouteStore.for_run(project, 42).violations()) == 2


def test_dispatch_preserves_direct_work_scope_authorization(project):
    from frisket.engine.store.execution_routes import instance_principal
    from frisket.engine.store.runs import RunResultStore
    from frisket.execution.attempt_authority import admit_routed
    from frisket.execution.promise_compiler import (
        OperatorBorneZeroCost,
        compile_route_promises,
    )
    from frisket.execution.resolve_for_action import (
        action_identity_hash,
        promise_rows,
    )
    from frisket.execution.resolver import RouteRowFacts
    from frisket.execution.scope_identity import canonical_work_scope_binding
    from frisket.ops.base import RecipeInvocationHalt

    sheet_id = project.add_sheet("Scoped")
    column_id = project.add_column(sheet_id, "media", type="audio")
    [row_id] = project.add_rows(
        sheet_id,
        [{"media": "original-audio-revision"}],
        {"media": column_id},
    )
    request = ActionRequest(
        action_id="media.transcribe",
        idempotency_key="scoped-transcribe",
        scope={"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": [row_id]},
        params={"source": "media", "engine": "moss"},
        output_names={"text": "transcript", "segments": "transcript_segments"},
    )
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    _, queued, recipe = typed_queued_map_spec(bound)
    spec = queued.runner_spec_fn(bound.params)
    op_id = project.append_op("map", spec)
    run_store = RunResultStore(project)
    run_id = run_store.start_run(
        op_id,
        sheet_id,
        "media.transcribe",
        params=spec,
        row_ids=[row_id],
    )
    work_scope = canonical_work_scope_binding(
        project,
        recipe,
        spec,
        [row_id],
    )
    facts = RouteRowFacts(
        target_id="models-gateway",
        engine="moss",
        operator="self",
        egress_class="operator_lan",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
    )
    compiled = compile_route_promises(
        facts,
        OperatorBorneZeroCost(),
        work_scope=work_scope,
    )
    store = RouteStore.for_run(project, run_id)
    promise_set = store.append_promise_set(
        promises=promise_rows(compiled),
        predecessor_id=None,
    )
    store.record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=promise_set.promise_set_hash,
        actor=instance_principal(project),
    )
    store.append_route(
        promise_set_id=promise_set.id,
        engine="moss",
        options={},
        target_snapshot=_snapshot("models-gateway", "frisket.transcription.v1"),
        operator=facts.operator,
        egress_class=facts.egress_class,
        region=facts.region,
        credential_source=facts.credential_source,
        cost_posture=facts.cost_posture,
        predecessor_id=None,
    )
    provider = _OneTargetProvider(
        ConnectionConfig(base_url="http://gw:9000", token="secret")
    )

    admitted = admit_routed(
        project,
        run_id,
        spec,
        composition=_composition(provider),
        recipe=recipe,
        scope=(row_id,),
    )
    assert admitted.route.subject_id == str(run_id)

    replace_test_source_cell(
        project,
        row_id=row_id,
        column_id=column_id,
        value="changed-audio-revision",
    )

    with pytest.raises(RecipeInvocationHalt) as exc_info:
        admit_routed(
            project,
            run_id,
            spec,
            composition=_composition(provider),
            recipe=recipe,
            scope=(row_id,),
        )
    assert exc_info.value.code == "promise_violation"
    assert "work scope" in str(exc_info.value)


def test_route_and_binding_are_one_value_not_two_extras_keys(project):
    """B3/B4: the four route-binding extras collapsed into ONE attempt key,
    and the ``require_route_binding`` fence has nothing left to fence — a
    ``RoutedAdmission`` carries route AND binding or does not exist."""
    from frisket.execution.attempt import (
        ATTEMPT_EXTRA,
        AttemptCommitment,
        routed_admission_in_scope,
    )
    from frisket.execution.attempt_authority import admit_routed
    from frisket.execution.promise_compiler import OperatorBorneZeroCost

    route = _persist_route(project, 43)
    admission = admit_routed(
        project,
        43,
        ADMIT_SPEC,
        composition=_composition(
            _OneTargetProvider(
                ConnectionConfig(base_url="http://gw:9000", token="secret")
            )
        ),
    )
    commitment = AttemptCommitment(
        attempt_id="attempt_test",
        run_id=43,
        seq=0,
        identity="identity",
        scope=(1,),
        admission=admission,
        cost_basis=OperatorBorneZeroCost(),
        price_card_version=None,
    )
    in_scope = routed_admission_in_scope({ATTEMPT_EXTRA: commitment})
    assert in_scope is admission
    assert in_scope.route.id == route.id
    assert in_scope.binding is admission.binding
    # No attempt in scope -> no route in scope. Type, not shape: a dict
    # cannot pose as a commitment.
    assert routed_admission_in_scope({}) is None
    assert routed_admission_in_scope(None) is None
    assert routed_admission_in_scope({ATTEMPT_EXTRA: {"route": route}}) is None
