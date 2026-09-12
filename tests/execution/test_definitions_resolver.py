"""Static definitions and resolver goldens.

Pins: the per-engine 1:1 resolution goldens (target id, transport, resolved
route facts), the liveness refusal families + remedies, the O1
short-circuit (a local resolution never probes gateway), and the
candidate-binding honesty rules (deref-only, never re-choose).
"""

from __future__ import annotations

import pytest

from frisket.execution.provider import CompositionFacts
from frisket.execution.resolver import (
    Refusal,
    ResolvedExecution,
    Resolution,
    ResolutionRequest,
    RouteRowFacts,
    candidate_binding,
    resolve,
)
from frisket.execution.promise_compiler import OperatorBorneZeroCost
from frisket.execution.definitions import (
    StaticExecutionTargetProvider,
    build_static_targets,
    parakeet_artifacts_present,
)
from frisket.execution.promises import PromiseSet

# Every activation env name the definitions read, plus the HF cache selectors
# the parakeet probe resolves through — cleared per test so the host machine's
# real config can never leak into a golden.
_ACTIVATION_ENV = (
    "FRISKET_MODELS_URL",
    "FRISKET_MODELS_TOKEN",
    "FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "HF_HOME",
)

OPEN = CompositionFacts()


@pytest.fixture(autouse=True)
def _clean_activation_env(monkeypatch, tmp_path):
    for name in _ACTIVATION_ENV:
        monkeypatch.delenv(name, raising=False)
    # An empty, isolated Hub cache by default: parakeet artifacts absent.
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    # Resolver goldens model an installation with the optional ASR runtime;
    # individual availability tests override this to exercise a missing extra.
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    yield


def _install_parakeet_artifacts(tmp_path, monkeypatch) -> None:
    """Fabricate the two pinned snapshots in a tmp Hub cache layout."""
    from frisket.ai.models import artifact_manifest

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


def _activate_everything(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-bearer")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic")
    _install_parakeet_artifacts(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# Per-authored-symbol resolution goldens for the public edition. The roster
# contains canonical names plus the advertised ``whisper`` alias, which
# canonicalizes to ``faster_whisper`` and takes the same resolution path.
# Retired spellings refuse below.


@pytest.mark.parametrize(
    ("authored", "engine_id", "target_id", "transport", "operator", "egress"),
    [
        # canonical names (option-free): local-first
        ("faster_whisper", "faster_whisper", "local", "local", "self", "none"),
        (
            "whisper-turbo",
            "whisper-turbo",
            "models-gateway",
            "frisket.transcription.v1",
            "self",
            "operator_lan",
        ),
        ("parakeet-tdt", "parakeet-tdt", "local-onnx", "local", "self", "none"),
        (
            "moss",
            "moss",
            "models-gateway",
            "frisket.transcription.v1",
            "self",
            "operator_lan",
        ),
        (
            "vibevoice-asr",
            "vibevoice-asr",
            "models-gateway",
            "frisket.transcription.v1",
            "self",
            "operator_lan",
        ),
        # the advertised alias: canonicalizes, then walks local-first
        ("whisper", "faster_whisper", "local", "local", "self", "none"),
    ],
)
def test_symbolic_engine_goldens(
    monkeypatch, tmp_path, authored, engine_id, target_id, transport, operator, egress
):
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine=authored), provider, OPEN)
    assert isinstance(result, Resolution)
    assert result.target.id == target_id
    assert result.support.engine == engine_id
    assert result.support.transport == transport
    # Resolved route facts (the route-row columns), open-edition rules:
    assert result.facts == RouteRowFacts(
        target_id=target_id,
        engine=engine_id,
        operator=operator,
        egress_class=egress,
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
    )


# ---------------------------------------------------------------------------
# Option/size-driven preference for fresh canonical names.


def test_local_whisper_and_fixed_turbo_are_distinct_engines():
    targets = {target.id: target for target in build_static_targets()}
    local = next(
        item for item in targets["local"].engines if item.engine == "faster_whisper"
    )
    gateway = next(
        item
        for item in targets["models-gateway"].engines
        if item.engine == "whisper-turbo"
    )
    assert local.options.model_size is True
    assert local.sizes == ("tiny", "base", "small", "medium")
    assert gateway.options.model_size is False
    assert gateway.sizes == ()


def test_glm_ocr_is_a_text_only_gateway_engine() -> None:
    targets = {target.id: target for target in build_static_targets()}
    glm = next(
        item for item in targets["models-gateway"].engines if item.engine == "glm-ocr"
    )

    assert glm.transport == "sidecar.ocr"
    assert glm.options.language is False
    assert glm.options.geometry is False


def test_glm_ocr_refuses_searchable_pdf_without_geometry(monkeypatch, tmp_path) -> None:
    _activate_everything(monkeypatch, tmp_path)

    result = resolve(
        ResolutionRequest(
            engine="glm-ocr",
            capability="ocr",
            options={"searchable_pdf": True},
        ),
        StaticExecutionTargetProvider(),
        OPEN,
    )

    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert "no text geometry" in result.remedy


def test_gateway_declares_parakeet_on_transcription_v1():
    targets = {target.id: target for target in build_static_targets()}
    gateway = {
        support.engine
        for support in targets["models-gateway"].engines
        if support.capability == "transcribe"
    }
    assert {"whisper-turbo", "parakeet-tdt", "moss"} <= gateway
    parakeet = next(
        support
        for support in targets["models-gateway"].engines
        if support.capability == "transcribe" and support.engine == "parakeet-tdt"
    )
    assert parakeet.transport == "frisket.transcription.v1"
    assert parakeet.options.diarization_mode == "optional"


def test_fresh_parakeet_tdt_with_diarize_refuses_on_local_target(monkeypatch, tmp_path):
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="parakeet-tdt", options={"diarize": True}),
        provider,
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert result.target_id == "local-onnx"
    assert "diarization" in result.remedy


def test_fresh_faster_whisper_large_size_refuses_no_capable_target(
    monkeypatch, tmp_path
):
    # The declared-capability rule: both targets expose their exact size
    # vocabulary. A canonical `faster_whisper` with an authored size resolves
    # local-only; a size beyond the local declared set is an honest
    # no_capable_target — NEVER a gateway route that would silently serve a
    # different weight, and NEVER a liveness-dependent answer (both targets
    # are live here).
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="faster_whisper", options={"model_size": "large-v3"}),
        provider,
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    # The honest remedy names the sole target with an authorable size knob.
    assert "tiny/base/small/medium" in result.remedy
    # Ability is static: no liveness probe was spent deciding this.
    assert provider.probe_counts == {}


def test_fresh_faster_whisper_local_size_stays_local(monkeypatch, tmp_path):
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="faster_whisper", options={"model_size": "base"}),
        provider,
        OPEN,
    )
    assert isinstance(result, Resolution)
    assert result.target.id == "local"
    # O1: the gateway was never probed.
    assert provider.probe_counts == {"local": 1}


@pytest.mark.parametrize(
    "spelling",
    [
        "local",
        "sidecar",
        "quality",
        "faster-whisper",
        "parakeet",
        "parakeet_modal",
        "modal",
        "remote",
    ],
)
def test_dead_spelling_refuses_even_with_everything_live(
    monkeypatch, tmp_path, spelling
):
    # Retired venue and target spellings are no longer accepted. Even with
    # every target live, resolution refuses — a dead name is unknown to the
    # roster (validation upstream rejects it with the canonical replacement
    # named; the resolver's refusal is the belt-and-suspenders behind it).
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine=spelling), provider, OPEN)
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    # No liveness probe is ever spent on an unknown name.
    assert provider.probe_counts == {}


def test_fresh_unknown_size_is_no_capable_target(monkeypatch, tmp_path):
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(
            engine="faster_whisper", options={"model_size": "colossal-v9"}
        ),
        provider,
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert "colossal-v9" in result.remedy


def test_raw_whisper_with_unservable_size_refuses(monkeypatch, tmp_path):
    # Ruling the resolver rule (kill check_sizes=False): an option no declaring target can
    # honor REFUSES rather than being ignored — never a silent substitution
    # of a smaller weight, never a venue flip. 'whisper' is the advertised
    # alias for faster_whisper; neither declaring target serves large-v3.
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="whisper", options={"model_size": "large-v3"}),
        provider,
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert "large-v3" in result.remedy


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"model_size": "base"},
        {"model_size": "medium"},
        {"model_size": "large-v3"},
        {"vad": True},
        {"language": "en"},
        {"diarize": True},
        {"model_size": "tiny", "vad": True, "language": "en"},
        {"model_size": "colossal-v9"},
    ],
)
def test_whisper_alias_resolves_identically_to_faster_whisper(
    monkeypatch, tmp_path, options
):
    """the route-bundle cutover deletion pin (RAW_SYMBOL_TARGET_PINS): the alias no longer pins a
    target family, so it must land wherever the canonical symbol lands — the
    same venue for every option set that resolves, and the same refusal
    FAMILY for every option set that does not. This is the behavioral claim
    that let the pin table go."""
    _activate_everything(monkeypatch, tmp_path)
    alias = resolve(
        ResolutionRequest(engine="whisper", options=options),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    canonical = resolve(
        ResolutionRequest(engine="faster_whisper", options=options),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert type(alias) is type(canonical)
    if isinstance(alias, Resolution):
        assert alias.target.id == canonical.target.id == "local"
        assert alias.support.engine == canonical.support.engine
    else:
        assert alias.family == canonical.family


def test_diarize_refuses_before_liveness_when_local_cannot_honor_it(
    monkeypatch, tmp_path
):
    # The first declared Parakeet target is local ONNX. An optional control
    # cannot reroute it to the gateway, even when a gateway would be the only
    # target able to honor the option.
    _install_parakeet_artifacts(tmp_path, monkeypatch)  # local-onnx IS live
    result = resolve(
        ResolutionRequest(engine="parakeet-tdt", options={"diarize": True}),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert result.target_id == "local-onnx"
    assert "diarization" in result.remedy


def test_fresh_parakeet_tdt_dead_local_refuses_never_walks_to_gateway(
    monkeypatch, tmp_path
):
    # The deterministic-choice rule: VENUE NEVER DEPENDS ON LIVENESS. The deterministic static
    # choice for option-free canonical `parakeet-tdt` is local-onnx (first
    # able candidate in declaration order); with local-onnx dead and the gateway
    # fully live, resolution refuses no_live_target with the local remedy —
    # it NEVER walks past the dead candidate to the gateway (that venue flip
    # would be a claims/egress change policy classified as local).
    _activate_everything(monkeypatch, tmp_path)
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "empty-hub"))
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine="parakeet-tdt"), provider, OPEN)
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "local-onnx"
    assert "Download" in result.remedy or "download" in result.remedy
    # The live gateway target was never even probed (O1: liveness is checked
    # only for the chosen target).
    assert provider.probe_counts == {"local-onnx": 1}


def test_fresh_parakeet_tdt_all_targets_dead_refuses_with_first_remedy(
    monkeypatch, tmp_path
):
    result = resolve(
        ResolutionRequest(engine="parakeet-tdt"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    # The remedy belongs to the FIRST able preference (local-onnx): the
    # artifact download, not a gateway config hint.
    assert result.target_id == "local-onnx"
    assert "Download" in result.remedy or "download" in result.remedy


def test_provider_model_engine_resolves_remote_api_target(monkeypatch, tmp_path):
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="anthropic/some-speech-model"), provider, OPEN
    )
    assert isinstance(result, Resolution)
    assert result.target.id == "remote-api:anthropic"
    assert result.support.engine == "anthropic/some-speech-model"
    assert result.support.transport == "remote"
    assert result.facts.operator == "anthropic"
    assert result.facts.egress_class == "third_party_api"
    assert result.facts.credential_source == "local"
    assert result.facts.cost_posture == "operator_borne"


def test_openai_whisper_1_resolves_through_remote_api_openai(monkeypatch, tmp_path):
    # The provider-qualified id (the honest authored form; the old `remote`
    # placement word was deleted at the engine-roster cutover) resolves through the openai
    # remote-api target.
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine="openai/whisper-1"), provider, OPEN)
    assert isinstance(result, Resolution)
    assert result.target.id == "remote-api:openai"
    assert result.support.engine == "openai/whisper-1"
    assert result.facts.engine == "openai/whisper-1"


def test_parakeet_selected_support_is_run_scoped(monkeypatch, tmp_path):
    _install_parakeet_artifacts(tmp_path, monkeypatch)
    result = resolve(
        ResolutionRequest(engine="parakeet-tdt"), StaticExecutionTargetProvider(), OPEN
    )
    assert isinstance(result, Resolution)
    assert result.support.run_scoped is True
    assert result.support.options.language is False  # fixed-language engine


def test_local_estimate_basis_has_no_tariff(monkeypatch, tmp_path):
    from frisket.execution.price_book import sku_for

    result = resolve(
        ResolutionRequest(engine="faster_whisper"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Resolution)
    assert result.estimate_basis.hardware_class is None
    # "No tariff" is the price book's answer now, not a field on the basis.
    assert (
        sku_for(
            capability="transcribe",
            target_id=result.target.id,
            engine=result.facts.engine,
        )
        is None
    )


# ---------------------------------------------------------------------------
# Refusals.


def test_gateway_engines_refuse_without_env(monkeypatch, tmp_path):
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine="moss"), provider, OPEN)
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "models-gateway"
    assert "FRISKET_MODELS_URL" in result.remedy
    assert "FRISKET_MODELS_TOKEN" in result.remedy


def test_gateway_refuses_with_url_but_no_token(monkeypatch, tmp_path):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    result = resolve(
        ResolutionRequest(engine="moss"), StaticExecutionTargetProvider(), OPEN
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"


def test_remote_api_refuses_without_provider_key(monkeypatch, tmp_path):
    result = resolve(
        ResolutionRequest(engine="openai/whisper-1"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "remote-api:openai"
    assert "OPENAI_API_KEY" in result.remedy


class _ProjectWithUiProviderKey:
    """A project as the AI Providers UI leaves it: the key lives ENCRYPTED in
    the `project_provider_keys` table (read back through
    `Project.provider_model_keys`), and Settings > Secrets is empty."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys
        self.secret_reads: list[str] = []

    def provider_model_keys(self) -> dict[str, str]:
        return dict(self._keys)

    def secret_plaintext(self, name: str) -> str | None:
        self.secret_reads.append(name)
        return None


def test_remote_api_sees_a_key_configured_in_the_ai_providers_ui(monkeypatch):
    """The AI Providers UI writes `project_provider_keys`; the probe used to
    read only env + project SECRETS, so a project whose OpenAI key was
    configured in the UI refused `no_live_target` for every VLM OCR and
    remote transcription run. `Project.provider_model_keys` is the same seam
    both key brokers layer onto their routers."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    project = _ProjectWithUiProviderKey({"openai": "sk-project"})
    provider = StaticExecutionTargetProvider(secrets=project)

    connection = provider.connection("remote-api:openai")

    assert connection is not None
    assert connection.token == "sk-project"


def test_remote_api_prefers_the_project_key_over_env(monkeypatch):
    """Both brokers merge `{**env_keys, **project_keys}`, so the project key
    is the one the call will actually run under. The probe must predict THAT
    key, not the one it happens to find first."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    project = _ProjectWithUiProviderKey({"openai": "sk-project"})
    provider = StaticExecutionTargetProvider(secrets=project)

    connection = provider.connection("remote-api:openai")

    assert connection is not None
    assert connection.token == "sk-project"


def test_remote_api_no_longer_reports_live_off_a_project_secret(monkeypatch):
    """A project SECRET named OPENAI_API_KEY is not a router key: the remote
    transcription and VLM OCR effects both take their credential off
    `router.adapter_for(provider)`, and no broker feeds project secrets into
    a router. Probing it reported LIVE for a key dispatch would never use."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    class _SecretsOnlyProject:
        def provider_model_keys(self) -> dict[str, str]:
            return {}

        def secret_plaintext(self, name: str) -> str | None:
            return "sk-secret" if name == "OPENAI_API_KEY" else None

    provider = StaticExecutionTargetProvider(secrets=_SecretsOnlyProject())

    assert provider.connection("remote-api:openai") is None


def test_remote_api_key_decrypt_failure_is_loud(monkeypatch):
    """A stored key that will not decrypt must not degrade to
    `no_live_target`: "you have no key" is the wrong sentence for a key that
    is right there, and the run would otherwise silently fall through to the
    env key — spending against a credential with no per-key cap."""
    from frisket.engine.store.credentials import ProviderKeyDecryptError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    class _UndecryptableProject:
        def provider_model_keys(self) -> dict[str, str]:
            raise ProviderKeyDecryptError("openai")

    provider = StaticExecutionTargetProvider(secrets=_UndecryptableProject())

    with pytest.raises(ProviderKeyDecryptError) as excinfo:
        provider.connection("remote-api:openai")
    assert "openai" in str(excinfo.value)


def test_remote_api_liveness_remedy_names_the_capability(monkeypatch):
    """routed OCR gave VLM OCR the same `remote-api:{provider}` rows the transcription
    engines ride, and the remedy was hardcoded to "transcription" — so an OCR
    run's refusal told the operator to enable a feature they were not using.
    """
    for name in ("OPENAI_API_KEY",):
        monkeypatch.delenv(name, raising=False)
    provider = StaticExecutionTargetProvider()

    ocr = resolve(
        ResolutionRequest(engine="openai/gpt-4.1-mini", capability="ocr"),
        provider,
        OPEN,
    )
    assert isinstance(ocr, Refusal)
    assert ocr.family == "no_live_target"
    assert "OCR" in ocr.remedy
    assert "transcription" not in ocr.remedy

    transcription = resolve(
        ResolutionRequest(engine="openai/whisper-1", capability="transcribe"),
        provider,
        OPEN,
    )
    assert isinstance(transcription, Refusal)
    assert "transcription" in transcription.remedy
    assert "OCR" not in transcription.remedy


def test_liveness_remedy_hook_predating_capability_still_answers():
    """The port documents `liveness_remedy` as an OPTIONAL duck-typed
    enrichment and a downstream composition ships its own provider, so a
    one-argument hook must keep working rather than TypeError'ing the
    refusal."""
    from frisket.execution.resolver import _liveness_remedy

    class _OldProvider:
        def liveness_remedy(self, target_id: str) -> str:
            return f"old-style remedy for {target_id}"

    assert (
        _liveness_remedy(_OldProvider(), "remote-api:openai", "ocr")
        == "old-style remedy for remote-api:openai"
    )


def test_unknown_engine_is_no_capable_target():
    result = resolve(
        ResolutionRequest(engine="quantum_whisper"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"


@pytest.mark.parametrize(
    ("options", "inability"),
    [
        ({"vad": True}, "voice-activity detection"),
        ({"model_size": "large-v3"}, "model size selection"),
        ({"diarize": True}, "diarization"),
        ({"num_speakers": 2}, "speaker-count hints"),
        ({"context": "Frisket"}, "context (hotword) prompt"),
    ],
)
def test_provider_model_engine_unsupported_knob_refuses(
    monkeypatch, tmp_path, options, inability
):
    # the engine-roster cutover / audit G4: the free-form remote-api branch consults the SAME
    # ability checker as the canonical branch — an authored knob the
    # remote-transport support row does not declare refuses honestly at
    # resolution (authoring validation refuses it too), never a silent
    # drop on the wire.
    _activate_everything(monkeypatch, tmp_path)
    provider = StaticExecutionTargetProvider()
    result = resolve(
        ResolutionRequest(engine="openai/whisper-1", options=options),
        provider,
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"
    assert result.target_id == "remote-api:openai"
    assert inability in result.remedy


def test_unknown_provider_prefix_is_no_capable_target():
    result = resolve(
        ResolutionRequest(engine="acme/speech-9"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(result, Refusal)
    assert result.family == "no_capable_target"


def test_hosted_edition_resolves_funding_into_the_route_facts():
    """The ``ValueError`` tripwire became a real branch. The
    composition's funding decides the route row's cost posture AND the
    credential family — the two facts a hosted run's user is actually
    consenting to."""
    from frisket.execution.price_book import ByokZero, PlatformMetered

    metered = resolve(
        ResolutionRequest(engine="faster_whisper"),
        StaticExecutionTargetProvider(),
        CompositionFacts(edition="hosted", funding=PlatformMetered()),
    )
    assert isinstance(metered, Resolution)
    assert metered.facts.cost_posture == "platform_metered"
    assert metered.facts.credential_source == "platform_key"

    byok = resolve(
        ResolutionRequest(engine="faster_whisper"),
        StaticExecutionTargetProvider(),
        CompositionFacts(edition="hosted", funding=ByokZero()),
    )
    assert isinstance(byok, Resolution)
    assert byok.facts.cost_posture == "org_key"
    assert byok.facts.credential_source == "org_byok"


def test_an_unknown_edition_is_a_programming_error_not_a_refusal():
    with pytest.raises(ValueError):
        resolve(
            ResolutionRequest(engine="faster_whisper"),
            StaticExecutionTargetProvider(),
            CompositionFacts(edition="team-preview"),  # type: ignore[arg-type]
        )


def test_the_open_edition_cannot_be_composed_with_a_billing_funding_class():
    """A self-hosted install bills nobody: an open composition that claims to
    meter the user is refused at construction, not silently rated."""
    from frisket.execution.price_book import PlatformMetered

    with pytest.raises(ValueError, match="operator-borne by construction"):
        CompositionFacts(edition="open", funding=PlatformMetered())


# ---------------------------------------------------------------------------
# O1 short-circuit: local resolution never probes elsewhere.


def test_local_resolution_probes_only_the_local_target(monkeypatch):
    import frisket.execution.definitions as definitions

    calls = {"parakeet_probe": 0}
    real_probe = definitions.parakeet_artifacts_present

    def counting_probe():
        calls["parakeet_probe"] += 1
        return real_probe()

    monkeypatch.setattr(definitions, "parakeet_artifacts_present", counting_probe)
    provider = StaticExecutionTargetProvider()
    result = resolve(ResolutionRequest(engine="faster_whisper"), provider, OPEN)
    assert isinstance(result, Resolution)
    # Exactly one liveness probe — the mapped target's; gateway/remote
    # definitions were never consulted, nor was the artifact probe.
    assert provider.probe_counts == {"local": 1}
    assert calls["parakeet_probe"] == 0


def test_installing_the_artifacts_makes_local_onnx_live_with_no_reset(
    monkeypatch, tmp_path
):
    """The download the local-onnx remedy names must make local-onnx LIVE.

    This is the whole flow ``liveness_remedy`` sends the user through, in the
    order it actually happens:

      1. the Models/action-catalog page loads and probes (artifacts absent);
      2. the operator downloads them (in the QUEUE-WORKER process, which is
         why nothing the web process could call would learn about it);
      3. the operator retries the transcription.

    A per-process cache populated at (1) answered False forever, so (3)
    routed ``parakeet-tdt`` past the dead local venue to a PAID one while the
    Models page showed the artifacts installed. This test replaces
    ``test_parakeet_probe_result_is_cached_per_process``, which asserted
    exactly the stale answer as the contract; there is no cache to reset now,
    and there is deliberately no reset hook to call here.
    """
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    assert parakeet_artifacts_present() is False
    assert StaticExecutionTargetProvider().connection("local-onnx") is None

    _install_parakeet_artifacts(tmp_path, monkeypatch)

    assert parakeet_artifacts_present() is True
    assert StaticExecutionTargetProvider().connection("local-onnx") is not None
    outcome = resolve(
        ResolutionRequest(engine="parakeet-tdt"),
        StaticExecutionTargetProvider(),
        OPEN,
    )
    assert isinstance(outcome, Resolution)
    assert outcome.target.id == "local-onnx"


def test_local_onnx_refuses_when_the_asr_runtime_is_missing(monkeypatch, tmp_path):
    _install_parakeet_artifacts(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: False
    )

    provider = StaticExecutionTargetProvider()
    assert provider.connection("local-onnx") is None
    assert "pip install 'frisket-data[standard]'" in (
        provider.liveness_remedy("local-onnx") or ""
    )


def test_uninstalling_the_artifacts_makes_local_onnx_dead_again(monkeypatch, tmp_path):
    """The mirror direction: the answer may not outlive the bytes either."""
    _install_parakeet_artifacts(tmp_path, monkeypatch)
    assert parakeet_artifacts_present() is True
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "now-empty"))
    assert parakeet_artifacts_present() is False
    assert StaticExecutionTargetProvider().connection("local-onnx") is None


# ---------------------------------------------------------------------------
# candidate_binding honesty: deref by target_id only, never re-choose.


def _gateway_facts(target_id: str = "models-gateway") -> RouteRowFacts:
    return RouteRowFacts(
        target_id=target_id,
        engine="moss",
        operator="self",
        egress_class="operator_lan",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
    )


def test_candidate_binding_derefs_live_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://models.test:8500")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "gateway-bearer")
    provider = StaticExecutionTargetProvider()
    binding = candidate_binding(_gateway_facts(), provider)
    assert not isinstance(binding, Refusal)
    assert binding.facts.target_id == "models-gateway"
    assert binding.connection.base_url == "http://models.test:8500"
    assert binding.connection.token == "gateway-bearer"
    assert binding.target.id == "models-gateway"


def test_candidate_binding_stale_target_id_is_no_live_target():
    provider = StaticExecutionTargetProvider()
    result = candidate_binding(_gateway_facts("models-gateway-v0"), provider)
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "models-gateway-v0"


def test_candidate_binding_dead_target_refuses_with_remedy():
    provider = StaticExecutionTargetProvider()
    result = candidate_binding(_gateway_facts(), provider)
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert "FRISKET_MODELS_URL" in result.remedy


def test_candidate_binding_never_re_chooses(monkeypatch, tmp_path):
    # Even with a perfectly live local target on offer, a route pinned to a
    # dead target refuses — it never silently re-resolves to another target.
    provider = StaticExecutionTargetProvider()
    facts = RouteRowFacts(
        target_id="local-onnx",
        engine="parakeet-tdt",
        operator="self",
        egress_class="none",
        region=None,
        credential_source="local",
        cost_posture="operator_borne",
    )
    result = candidate_binding(facts, provider)
    assert isinstance(result, Refusal)
    assert result.family == "no_live_target"
    assert result.target_id == "local-onnx"


# ---------------------------------------------------------------------------
# ResolvedExecution type (persistence contract home; writer enforcement is
# the routed capability registrations).


def test_resolved_execution_carries_persistence(monkeypatch, tmp_path):
    _install_parakeet_artifacts(tmp_path, monkeypatch)
    resolution = resolve(
        ResolutionRequest(engine="parakeet-tdt"), StaticExecutionTargetProvider(), OPEN
    )
    assert isinstance(resolution, Resolution)
    # promise_set/cost_basis are REQUIRED (E-6): "resolved but not compiled"
    # is no longer a representable state for the writer to catch later.
    compiled = PromiseSet.make(())
    preview = ResolvedExecution(
        resolution=resolution,
        persistence="ephemeral",
        promise_set=compiled,
        cost_basis=OperatorBorneZeroCost(),
    )
    durable = ResolvedExecution(
        resolution=resolution,
        persistence="durable",
        promise_set=compiled,
        cost_basis=OperatorBorneZeroCost(),
    )
    with pytest.raises(TypeError):
        ResolvedExecution(resolution=resolution, persistence="durable")
    assert preview.persistence == "ephemeral"
    assert durable.resolution is resolution


def test_local_whisper_and_gateway_turbo_never_share_an_engine_identity():
    targets = build_static_targets()
    by_id = {target.id: target for target in targets}
    local_engines = {support.engine for support in by_id["local"].engines}
    gateway_engines = {support.engine for support in by_id["models-gateway"].engines}
    assert "faster_whisper" in local_engines
    assert "faster_whisper" not in gateway_engines
    assert "whisper-turbo" in gateway_engines
    assert "whisper-turbo" not in local_engines
