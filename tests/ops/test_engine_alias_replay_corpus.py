"""Typed engine identity, live aliases, target preference and provider claims."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from frisket.actions.media import OcrParams, TranscribeParams, transcribe_options
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.map_rows_action import typed_queued_map_spec
from frisket.execution.action_identity import action_identity_hash
from frisket.contracts.actions.schemas._engines import (
    OCR_DEAD_ENGINE_REPLACEMENTS,
    TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS,
    TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS,
    TRANSCRIBE_ENGINE_TABLE,
    receipt_provider_map,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.sdk.ops.transcribe_engines import ENGINE_ALIASES


@dataclass(frozen=True)
class CorpusEntry:
    """One frozen spelling: its raw wire form and every derived identity."""

    spelling: str  # exactly what the saved spec says (never rewritten)
    resolved_engine: str  # execution-time resolution (ENGINE_ALIASES)
    receipt_provider: str  # provider_use grouping (receipt_provider_map)
    # Target preservation: the execution venue this spelling
    # resolves to, with its consequences — pinned so table reshaping can
    # never silently flip a saved spec's venue, cost posture, or claims.
    resolved_target: str  # resolver target id (raw-symbol preference)
    transport: str  # the selected TargetEngineSupport transport
    cost_posture: str  # open-edition resolved route fact
    claims: frozenset[str]  # user_claim fields the gate would show


# --------------------------------------------------------------------------- #
# Live engine roster and its route/provider expectations.
# --------------------------------------------------------------------------- #
ALIAS_REPLAY_CORPUS: tuple[CorpusEntry, ...] = (
    # -- the advertised convenience alias (pins the local worker) --
    CorpusEntry(
        spelling="whisper",
        resolved_engine="faster_whisper",
        receipt_provider="local",
        resolved_target="local",
        transport="local",
        cost_posture="operator_borne",
        claims=frozenset(),
    ),
    # -- canonical ids (identity resolution; pinned so a future table change
    #    cannot silently re-key them either) --
    CorpusEntry(
        spelling="faster_whisper",
        resolved_engine="faster_whisper",
        receipt_provider="local",
        resolved_target="local",
        transport="local",
        cost_posture="operator_borne",
        claims=frozenset(),
    ),
    CorpusEntry(
        spelling="moss",
        resolved_engine="moss",
        receipt_provider="frisket-sidecar",
        resolved_target="models-gateway",
        transport="frisket.transcription.v1",
        cost_posture="operator_borne",
        claims=frozenset({"egress_class"}),
    ),
    # -- APPENDED during target resolution: the collapsed canonical spelling. Fresh
    #    canonical resolution is option-driven; with no options it is
    #    local-onnx-first. --
    CorpusEntry(
        spelling="parakeet-tdt",
        resolved_engine="parakeet-tdt",
        receipt_provider="local-onnx",
        resolved_target="local-onnx",
        transport="local",
        cost_posture="operator_borne",
        claims=frozenset(),
    ),
    # -- APPENDED with the fixed gateway Whisper Turbo roster entry. --
    CorpusEntry(
        spelling="whisper-turbo",
        resolved_engine="whisper-turbo",
        receipt_provider="self",
        resolved_target="models-gateway",
        transport="frisket.transcription.v1",
        cost_posture="operator_borne",
        claims=frozenset({"egress_class"}),
    ),
    # -- APPENDED with the hosted MAI OpenRouter roster entry. --
    CorpusEntry(
        spelling="openrouter/microsoft/mai-transcribe-2",
        resolved_engine="openrouter/microsoft/mai-transcribe-2",
        receipt_provider="openrouter",
        resolved_target="remote-api:openrouter",
        transport="remote",
        cost_posture="operator_borne",
        claims=frozenset({"egress_class", "cost"}),
    ),
    # -- APPENDED with the fixed VibeVoice-ASR gateway profile. --
    CorpusEntry(
        spelling="vibevoice-asr",
        resolved_engine="vibevoice-asr",
        receipt_provider="frisket-sidecar",
        resolved_target="models-gateway",
        transport="frisket.transcription.v1",
        cost_posture="operator_borne",
        claims=frozenset({"egress_class"}),
    ),
)


def _corpus_spec(spelling: str) -> ActionRequest:
    params = TranscribeParams(source="media", engine=spelling)
    action = ACTION_REGISTRY.get("media.transcribe")
    outputs = action.definition.run.resolve_output_fields(params)
    return ActionRequest(
        action_id=action.action_id,
        idempotency_key="alias-replay@sha256:corpus",
        scope={"kind": "sheet_rows", "sheet_id": 7},
        params=params.model_dump(mode="json", exclude_unset=True),
        output_names={field.key: field.key for field in outputs},
    )


def _identity(request: ActionRequest) -> str:
    action = ACTION_REGISTRY.get(request.action_id)
    bound = BoundTypedActionRequest.bind(action, request)
    _, queued, _ = typed_queued_map_spec(bound)
    return action_identity_hash(queued.runner_spec_fn(bound.params))


def _entry_id(entry: CorpusEntry) -> str:
    return entry.spelling


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_spelling_validates_as_transcribe_params(entry: CorpusEntry):
    validated = TranscribeParams(source="media", engine=entry.spelling)
    assert validated.engine.root == entry.spelling


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_canonical_params_preserve_the_raw_spelling(entry: CorpusEntry):
    request = _corpus_spec(entry.spelling)
    assert request.params["engine"] == entry.spelling


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_typed_identity_round_trips(entry: CorpusEntry):
    request = _corpus_spec(entry.spelling)
    assert _identity(request) == _identity(
        ActionRequest.model_validate_json(request.model_dump_json())
    )


def test_corpus_authored_spellings_have_distinct_identities():
    hashes = [_identity(_corpus_spec(entry.spelling)) for entry in ALIAS_REPLAY_CORPUS]
    assert len(hashes) == len(set(hashes))


def test_true_alias_has_identical_dispatch_options():
    alias = TranscribeParams(source="media", engine="whisper")
    canonical = TranscribeParams(source="media", engine="faster_whisper")
    assert transcribe_options(alias).normalize(alias.engine.root) == transcribe_options(
        canonical
    ).normalize(canonical.engine.root)


def test_authored_option_changes_typed_identity():
    request = _corpus_spec("faster_whisper")
    changed = request.model_copy(update={"params": {**request.params, "vad": False}})
    assert _identity(request) != _identity(changed)


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_execution_resolution_is_stable(entry: CorpusEntry):
    # (d) Execution-time resolution (ops layer, ENGINE_ALIASES) maps each
    # spelling to the same engine it always ran on.
    assert ENGINE_ALIASES.get(entry.spelling, entry.spelling) == (entry.resolved_engine)


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_receipt_provider_grouping_is_stable(entry: CorpusEntry):
    # (e) provider_use grouping on receipts is stable per spelling.
    providers = receipt_provider_map(TRANSCRIBE_ENGINE_TABLE)
    assert providers[entry.spelling] == entry.receipt_provider


def test_corpus_covers_every_table_spelling():
    # Coverage guard against the CURRENT roster truth: every id + alias in
    # the live table must appear in the corpus; and no table spelling may be
    # a dead name — resurrecting one requires the deliberate product decision
    # of deleting it from the teaching map first (the _engines.py
    # module-level collision guard enforces the same rule at import).
    table_spellings = set()
    for declaration in TRANSCRIBE_ENGINE_TABLE:
        table_spellings.add(declaration.id)
        table_spellings.update(declaration.aliases)
    corpus_spellings = {entry.spelling for entry in ALIAS_REPLAY_CORPUS}
    assert table_spellings <= corpus_spellings, (
        "table spellings missing a frozen corpus entry: "
        f"{sorted(table_spellings - corpus_spellings)}"
    )
    assert not (table_spellings & set(TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS)), (
        "dead spellings re-appeared in the live table"
    )


# --------------------------------------------------------------------------- #
# The engine-roster cutover teaching rejections: a dead name refuses at validation with the
# canonical replacement named — UX copy, not compatibility machinery.
# --------------------------------------------------------------------------- #

_DEAD_NAME_CASES = (
    [
        pytest.param(
            TranscribeParams,
            "invalid_transcription_engine",
            spelling,
            replacement,
            id=f"transcribe-{spelling}",
        )
        for spelling, replacement in sorted(TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS.items())
    ]
    + [
        pytest.param(
            OcrParams,
            "invalid_ocr_engine",
            spelling,
            replacement,
            id=f"ocr-{spelling}",
        )
        for spelling, replacement in sorted(OCR_DEAD_ENGINE_REPLACEMENTS.items())
    ]
    + [
        pytest.param(
            ACTION_REGISTRY.get("media.to_markdown").definition.run.params_model,
            "invalid_to_markdown_engine",
            spelling,
            replacement,
            id=f"to_markdown-{spelling}",
        )
        for spelling, replacement in sorted(
            TO_MARKDOWN_DEAD_ENGINE_REPLACEMENTS.items()
        )
    ]
)


@pytest.mark.parametrize("model, code, spelling, replacement", _DEAD_NAME_CASES)
def test_dead_engine_name_rejects_with_the_replacement(
    model, code, spelling, replacement
):
    with pytest.raises(ValidationError) as excinfo:
        model.model_validate({"source": "media", "engine": spelling})
    message = str(excinfo.value)
    assert code in message
    assert f"engine '{spelling}' was retired; use '{replacement}'" in message


def test_dead_transcribe_spelling_never_resolves():
    # Belt-and-suspenders behind validation: the ops alias map no longer
    # rewrites dead spellings, and the resolver refuses them.
    from frisket.execution.definitions import StaticExecutionTargetProvider
    from frisket.execution.provider import CompositionFacts
    from frisket.execution.resolver import Refusal, ResolutionRequest, resolve

    provider = StaticExecutionTargetProvider()
    for spelling in TRANSCRIBE_DEAD_ENGINE_REPLACEMENTS:
        assert spelling not in ENGINE_ALIASES
        result = resolve(
            ResolutionRequest(engine=spelling), provider, CompositionFacts()
        )
        assert isinstance(result, Refusal), spelling
        assert result.family == "no_capable_target"


@pytest.fixture
def _all_targets_live(monkeypatch, tmp_path):
    """Every static target activated (env + fabricated local artifacts)
    so each spelling's target preference is observable, not masked by a
    liveness refusal."""
    from frisket.ai.models import artifact_manifest

    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )

    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-bearer")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter")
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
    yield


@pytest.mark.parametrize("entry", ALIAS_REPLAY_CORPUS, ids=_entry_id)
def test_corpus_target_preservation(entry: CorpusEntry, _all_targets_live):
    # Per authored symbol, the resolved execution
    # venue — target id, transport, cost posture, and the user-claim fields
    # the gate would show — is pinned, not merely the hash. A saved spec's
    # venue flip is a claims change: it may only ever happen through an
    # explicit table/preference edit that fails THIS test first.
    from frisket.execution.promise_compiler import (
        OperatorBorneZeroCost,
        UnpriceableCost,
        compile_route_promises,
    )
    from frisket.execution.definitions import StaticExecutionTargetProvider
    from frisket.execution.provider import CompositionFacts
    from frisket.execution.resolver import Resolution, ResolutionRequest, resolve

    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine=entry.spelling), provider, CompositionFacts()
    )
    assert isinstance(result, Resolution), result
    assert result.target.id == entry.resolved_target
    assert result.support.transport == entry.transport
    assert result.facts.cost_posture == entry.cost_posture
    # The claims set: hosted (modal/remote-api) venues bill — represented
    # here by the unpriceable basis (the priced basis yields the same
    # user-claim FIELDS); operator-owned venues are genuinely zero.
    hosted = entry.resolved_target.startswith(
        "modal:"
    ) or entry.resolved_target.startswith("remote-api:")
    cost = UnpriceableCost() if hosted else OperatorBorneZeroCost()
    promise_set = compile_route_promises(
        result.facts,
        cost,
    )
    user_claims = {
        promise.field
        for promise in promise_set.promises
        if promise.audience == "user_claim"
    }
    assert user_claims == set(entry.claims)


def test_typed_identity_is_insertion_order_invariant():
    request = _corpus_spec("faster_whisper")
    reordered = request.model_copy(
        update={"params": dict(reversed(list(request.params.items())))}
    )
    assert _identity(request) == _identity(reordered)
