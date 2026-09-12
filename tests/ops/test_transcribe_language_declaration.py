"""Engine-declared language, canonical wire params, detected-language gating,
and diarization parameters.

The checks pin canonical wire params, the distinct `fixed` language mode,
detected_language gating, the absence of per-word speaker data, and
diarization capability projection."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from frisket.actions.media import TRANSCRIBE, TranscribeParams, transcribe_options
from frisket.actions.media_options import TranscriptionOptions
from frisket.contracts.action import (
    LanguageChoice,
    LanguageDeclaration,
    canonicalize_languages,
    transcribe_max_speakers,
    transcribe_speaker_hint,
    transcribe_supports_diarization,
    transcribe_engine_capabilities,
)
from frisket.contracts.actions.schemas._language import AUTO_SENTINEL
from frisket.contracts.transcription_language import transcribe_language_declaration
from frisket.server.action_catalog_hints import (
    _recipe_engines,
    action_catalog_payload_with_launcher_hints,
)


# --------------------------------------------------------------------------- #
# Step 1: the declaration model


def test_language_declaration_round_trips():
    decl = LanguageDeclaration(
        mode="single",
        default="auto",
        choices=[LanguageChoice(value="en", label="English")],
        detects=True,
    )
    dumped = decl.model_dump()
    assert dumped["mode"] == "single"
    assert dumped["choices"] == [{"value": "en", "label": "English"}]
    assert LanguageDeclaration.model_validate(dumped) == decl
    # json schema must generate without error (catalog embeds it)
    assert LanguageDeclaration.model_json_schema()["properties"]["mode"]


def test_language_declaration_auto_only_and_fixed_allow_no_choices():
    LanguageDeclaration(mode="auto_only", detects=True)  # no choices required
    fixed = LanguageDeclaration(mode="fixed", default="en", fixed_language="en")
    assert fixed.choices is None
    assert fixed.detects is False


def test_fixed_is_a_distinct_mode_from_auto_only():
    # `fixed` is distinct because auto_only conflated
    # "detects any language" with "English only".
    assert set(LanguageDeclaration.model_fields["mode"].annotation.__args__) == {
        "auto_only",
        "fixed",
        "single",
        "multi",
    }


def test_canonicalize_languages_collapses_every_auto_spelling():
    # ONLY a wholly-auto request means auto -> []
    assert canonicalize_languages(None) == []
    assert canonicalize_languages([]) == []
    assert canonicalize_languages([AUTO_SENTINEL]) == []
    assert canonicalize_languages(["auto", "  ", ""]) == []
    # dedupe + sort real codes
    assert canonicalize_languages(["es", "en", "es"]) == ["en", "es"]
    # a bare string is accepted and normalised to a one-item list
    assert canonicalize_languages("en") == ["en"]


def test_canonicalize_languages_keeps_mixed_auto_for_loud_rejection():
    # A MIXED sentinel+code list is NOT silently coerced to
    # ["en"] — the sentinel is KEPT so validation rejects it loudly.
    assert canonicalize_languages(["fr", "auto", "de"]) == ["auto", "de", "fr"]
    assert canonicalize_languages(["auto", "en"]) == ["auto", "en"]


# --------------------------------------------------------------------------- #
# Step 1: per-engine declarations verified against ops/transcribe.py


def test_transcribe_engine_declarations_match_real_capability():
    # v3 chooses a supported language automatically but exposes neither a
    # caller override nor a detected-language result on this API.
    decl = transcribe_language_declaration("parakeet-tdt")
    assert decl.mode == "auto_only"
    assert decl.fixed_language is None
    assert decl.detects is False
    assert decl.choices is None
    # whisper family: single-language hint or auto, reports detection
    for engine in ("faster_whisper", "whisper"):
        decl = transcribe_language_declaration(engine)
        assert decl.mode == "single"
        assert decl.detects is True
        assert decl.choices  # populated Auto-first picker source
        assert all(c.value != AUTO_SENTINEL for c in decl.choices)
    # unknown provider/model id defaults to the whisper-class remote decl
    assert transcribe_language_declaration("openai/gpt-4o-transcribe").mode == "single"


def test_remote_detects_only_for_whisper_class_models():
    # The remote adapter only reads a detected language for
    # whisper-class models, so only they may advertise detected_language.
    assert transcribe_language_declaration("openai/whisper-1").detects is True
    assert transcribe_language_declaration("groq/whisper-large-v3").detects is True
    # non-whisper remote models: single picker, but NO detected_language column
    for model_id in ("openai/gpt-4o-transcribe", "deepgram/nova-2"):
        decl = transcribe_language_declaration(model_id)
        assert decl.mode == "single"
        assert decl.detects is False
    # and output_fields drops the column for a non-whisper remote engine
    params = TranscribeParams.model_validate(_params(engine="openai/gpt-4o-transcribe"))
    names = [field.key for field in TRANSCRIBE.run.resolve_output_fields(params)]
    assert "detected_language" not in names


def test_mai_transcribe_2_declares_its_extra_remote_controls():
    engine = "openrouter/microsoft/mai-transcribe-2"
    capabilities = transcribe_engine_capabilities(engine)
    assert capabilities.accepts_language is True
    assert capabilities.context is True
    assert capabilities.clean is True
    assert capabilities.diarization_mode == "optional"
    assert capabilities.speaker_hint == "none"

    defaulted = TranscribeParams.model_validate(_params(engine=engine))
    assert defaulted.clean is False
    assert transcribe_options(defaulted).normalize(engine)["diarize"] is True
    # An explicit false remains an instruction, rather than falling through
    # the model-specific default.
    explicit_false = TranscribeParams.model_validate(
        _params(engine=engine, diarize=False, clean=False)
    )
    assert explicit_false.diarize is False
    assert explicit_false.clean is False

    with pytest.raises(ValidationError, match="transcription_option_unavailable"):
        TranscribeParams.model_validate(_params(engine="openai/whisper-1", clean=True))


def test_no_transcribe_engine_declares_multi_today():
    # Whisper takes ONE language hint; declaring `multi` would render a control
    # the engine cannot honour; declarations may not silently truncate it.
    for engine in ("faster_whisper", "whisper", "parakeet-tdt"):
        assert transcribe_language_declaration(engine).mode != "multi"


def test_only_parakeet_tdt_supports_diarization():
    # Sortformer ships on the collapsed parakeet-tdt engine through
    # its models-gateway target. The ENGINE-level declaration is
    # the union of declared target support, so every spelling of the
    # collapsed engine reports the capability; per-target enforcement (the
    # local ONNX build does not diarize) happens at resolution and is
    # pinned by tests/execution/test_one_engine_two_targets.py.
    assert transcribe_supports_diarization("parakeet-tdt") is True
    assert transcribe_max_speakers("parakeet-tdt") == 4
    for engine in ("faster_whisper", "whisper"):
        assert transcribe_supports_diarization(engine) is False
        assert transcribe_max_speakers(engine) is None


def test_parakeet_tdt_declares_no_speaker_count_hint():
    # Sortformer's released offline checkpoint always runs its fixed 4-channel
    # pass with no count/range input -> "none", so the form renders
    # NO count field for it. The retired spellings resolve the same way.
    assert transcribe_speaker_hint("parakeet-tdt") == "none"
    # Non-diarizing engines fail closed with no speaker-count input.
    for engine in ("faster_whisper", "whisper"):
        assert transcribe_speaker_hint(engine) == "none"


# --------------------------------------------------------------------------- #
# Step 1: catalog projection


def test_recipe_engines_emit_language_and_diarization(monkeypatch):
    # Availability is live machine state: a developer may already have the
    # pinned Parakeet artifacts installed, and a gateway may be configured.
    # This test exercises the unavailable projection deliberately rather than
    # treating the machine running the suite as part of the fixture.
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_artifacts_present",
        lambda: False,
    )
    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}
    # The collapsed roster removes the old parakeet/parakeet_modal entries.
    # Hosted Whisper Turbo is one separately named fixed model.
    # The `remote` placement row is gone from the catalog.
    assert set(engines) == {
        "faster_whisper",
        "whisper-turbo",
        "parakeet-tdt",
        "moss",
        "vibevoice-asr",
        "openai/whisper-1",
        "openrouter/microsoft/mai-transcribe-2",
    }
    for engine in engines.values():
        assert "language" in engine
        assert engine["language"]["mode"] in {"auto_only", "fixed", "single", "multi"}
    # parakeet-tdt's gateway build ships Sortformer (supported + a 4-speaker
    # cap + a "none" speaker_hint since the released checkpoint takes no
    # count input) — the engine-level union declares it; the local ONNX
    # target declares none. MOSS and VibeVoice diarize intrinsically
    # (speaker-labeled output, no speaker knobs, no cap); every other engine
    # declares unsupported.
    assert engines["parakeet-tdt"]["diarization"] == {
        "supported": True,
        "mode": "optional",
        "max_speakers": 4,
        "speaker_hint": "none",
    }
    parakeet_targets = {t["target"]: t for t in engines["parakeet-tdt"]["targets"]}
    assert set(parakeet_targets) == {"local-onnx", "models-gateway"}
    assert parakeet_targets["local-onnx"]["diarization"]["supported"] is False
    assert parakeet_targets["models-gateway"]["diarization"]["supported"] is True
    # Per-target availability from the resolver's perspective: the fixture
    # declares neither artifacts nor a gateway configured, and the entry's
    # top-level availability reflects the PREFERRED target (local-onnx).
    assert parakeet_targets["local-onnx"]["available"] is False
    assert parakeet_targets["models-gateway"]["available"] is False
    assert engines["parakeet-tdt"]["available"] is False
    assert engines["moss"]["diarization"] == {
        "supported": True,
        "mode": "intrinsic",
        "speaker_hint": "none",
    }
    assert engines["moss"]["language"]["mode"] == "auto_only"
    assert engines["moss"]["transcription_options"] == {
        "language": False,
        "vad": False,
        "model_size": False,
        # The MOSS worker's hotword prompt is exposed end to end.
        "context": True,
        "clean": False,
    }
    # moss's availability comes from the gateway /capabilities probe; with no
    # sidecar configured it is honestly unavailable, never silently runnable.
    assert engines["moss"]["available"] is False
    assert engines["faster_whisper"]["diarization"] == {
        "supported": False,
        "mode": "none",
    }
    # Local Faster-Whisper keeps its existing selectable roster.
    whisper_targets = {t["target"]: t for t in engines["faster_whisper"]["targets"]}
    assert set(whisper_targets) == {"local"}
    assert whisper_targets["local"]["available"] is True
    assert whisper_targets["local"]["sizes"] == ["tiny", "base", "small", "medium"]
    turbo_targets = {t["target"]: t for t in engines["whisper-turbo"]["targets"]}
    assert set(turbo_targets) == {"models-gateway"}
    assert turbo_targets["models-gateway"]["available"] is False
    assert engines["faster_whisper"]["available"] is True
    assert engines["parakeet-tdt"]["language"]["mode"] == "auto_only"
    assert engines["parakeet-tdt"]["language"]["detects"] is False
    assert engines["faster_whisper"]["language"]["mode"] == "single"
    assert engines["faster_whisper"]["language"]["detects"] is True
    assert engines["faster_whisper"]["transcription_options"] == {
        "language": True,
        "vad": True,
        "model_size": True,
        "context": True,
        "clean": False,
    }
    assert engines["parakeet-tdt"]["transcription_options"] == {
        "language": False,
        "vad": True,
        "model_size": False,
        "context": False,
        "clean": False,
    }
    assert engines["openrouter/microsoft/mai-transcribe-2"][
        "transcription_options"
    ] == {
        "language": True,
        "vad": False,
        "model_size": False,
        "context": True,
        "clean": True,
    }
    assert engines["openrouter/microsoft/mai-transcribe-2"]["diarization"] == {
        "supported": True,
        "mode": "optional",
        "speaker_hint": "none",
        "default": True,
    }
    # Every standalone engine publishes exactly the target whose controls the
    # form may render; the browser never infers this from a union declaration.
    for engine in engines.values():
        target_id = engine["target_id"]
        [target] = [row for row in engine["targets"] if row["target_id"] == target_id]
        assert "transcription_options" in target
        assert "diarization" in target
    assert engines["moss"]["target_id"] == "models-gateway"
    assert engines["moss"]["targets"][0]["diarization"]["mode"] == "intrinsic"
    mai = engines["openrouter/microsoft/mai-transcribe-2"]
    assert mai["target_id"] == "remote-api:openrouter"
    assert mai["targets"] == [
        {
            "target": "remote-api:openrouter",
            "target_id": "remote-api:openrouter",
            "available": False,
            "error": "Configure an OpenRouter API key to enable Microsoft MAI-Transcribe 2.",
            "diarization": mai["diarization"],
            "transcription_options": mai["transcription_options"],
        }
    ]


def test_recipe_engines_disable_parakeet_when_the_asr_extra_is_missing(monkeypatch):
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_artifacts_present", lambda: True
    )
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: False
    )
    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}

    parakeet = engines["parakeet-tdt"]
    assert parakeet["available"] is False
    assert "pip install 'frisket-data[standard]'" in parakeet["error"]


def test_recipe_engines_disable_whisper_when_its_runtime_is_missing(monkeypatch):
    monkeypatch.setattr(
        "frisket.execution.definitions.faster_whisper_runtime_present", lambda: False
    )
    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}

    whisper = engines["faster_whisper"]
    assert whisper["available"] is False
    assert "pip install 'frisket-data[standard]'" in whisper["error"]


def test_openai_whisper_1_availability_follows_effective_provider_keys(
    monkeypatch, tmp_path
):
    """The provider-qualified id is the authorable hosted transcription form
    (the `remote` placement row was deleted). It stays authorable without a
    key, but the picker must not describe it as runnable until an effective
    OpenAI credential exists."""
    for env in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)

    whisper1 = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}[
        "openai/whisper-1"
    ]
    assert whisper1["tier"] == "hosted"
    assert whisper1["billable"] is True
    assert whisper1["available"] is False
    assert whisper1["label"] == "OpenAI Whisper (remote provider API)"
    assert whisper1["language"]["mode"] == "single"
    assert whisper1["language"]["detects"] is True
    assert whisper1["diarization"] == {"supported": False, "mode": "none"}
    # And the params contract accepts the id through its "/" validation path.
    validated = TranscribeParams.model_validate(
        _params(engine="openai/whisper-1", language=["fr"])
    )
    assert validated.engine.root == "openai/whisper-1"
    assert validated.language == ["fr"]

    class _ProjectWithProviderKey:
        path = tmp_path / "catalog.frisket"

        def provider_model_keys(self):
            return {"openai": "project-key"}

    project_whisper = {
        e["id"]: e
        for e in _recipe_engines(
            "media.transcribe", {}, project=_ProjectWithProviderKey()
        )
    }["openai/whisper-1"]
    assert project_whisper["available"] is True
    assert project_whisper.get("error") is None


def test_moss_availability_requires_v1_contract_advertisement():
    # A configured gateway advertising moss WITH the v1 contract → available.
    advertised = {
        "configured": True,
        "engines": [
            {
                "route": "/v1/transcribe",
                "name": "moss",
                "available": True,
                "models": ["OpenMOSS-Team/MOSS-Transcribe-Diarize"],
                "contract_versions": ["frisket.transcription.v1"],
            }
        ],
    }
    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", advertised)}
    assert engines["moss"]["available"] is True
    assert engines["moss"]["models"] == ["OpenMOSS-Team/MOSS-Transcribe-Diarize"]

    # An unversioned engine that merely shares the name must NOT read as
    # available: the app requires an explicit v1 advertisement.
    unversioned_same_name = {
        "configured": True,
        "engines": [
            {
                "route": "/v1/transcribe",
                "name": "moss",
                "available": True,
                "models": [],
            }
        ],
    }
    engines = {
        e["id"]: e for e in _recipe_engines("media.transcribe", unversioned_same_name)
    }
    assert engines["moss"]["available"] is False
    assert "frisket.transcription.v1" in engines["moss"]["error"]

    # Configured gateway with no moss advertisement → honest not-advertised.
    engines = {
        e["id"]: e
        for e in _recipe_engines(
            "media.transcribe", {"configured": True, "engines": []}
        )
    }
    assert engines["moss"]["available"] is False
    assert "not advertised" in engines["moss"]["error"]


def test_vibevoice_asr_is_an_honest_fixed_gateway_profile():
    advertised = {
        "configured": True,
        "engines": [
            {
                "route": "/v1/transcribe",
                "name": "vibevoice-asr",
                "available": True,
                "models": ["fixture/vibevoice-model"],
                "contract_versions": ["frisket.transcription.v1"],
            }
        ],
    }
    engine = {
        entry["id"]: entry for entry in _recipe_engines("media.transcribe", advertised)
    }["vibevoice-asr"]
    assert engine["available"] is True
    assert engine["tier"] == "sidecar"
    assert engine["language"] == {
        "mode": "auto_only",
        "default": "auto",
        "detects": False,
        "allows_auto": True,
        "choices": None,
        "fixed_language": None,
    }
    assert engine["diarization"] == {
        "supported": True,
        "mode": "intrinsic",
        "speaker_hint": "none",
    }
    assert engine["transcription_options"] == {
        "language": False,
        "vad": False,
        "model_size": False,
        "context": True,
        "clean": False,
    }
    assert engine["models"] == ["fixture/vibevoice-model"]

    for unsupported in ({"diarize": True}, {"num_speakers": 2}):
        with pytest.raises(ValidationError, match="transcription_option_unavailable"):
            TranscribeParams.model_validate(
                _params(engine="vibevoice-asr", **unsupported)
            )

    unavailable = {
        entry["id"]: entry
        for entry in _recipe_engines(
            "media.transcribe", {"configured": True, "engines": []}
        )
    }["vibevoice-asr"]
    assert unavailable["available"] is False
    assert "not advertised" in unavailable["error"]


@pytest.mark.parametrize(
    "contract_versions",
    [None, ["frisket.transcription.v2"]],
)
def test_whisper_turbo_gateway_requires_v1_contract_advertisement(
    contract_versions,
):
    advertisement = {
        "route": "/v1/transcribe",
        "name": "whisper-turbo",
        "available": True,
        "models": ["dropbox-dash/faster-whisper-large-v3-turbo"],
    }
    if contract_versions is not None:
        advertisement["contract_versions"] = contract_versions
    engines = {
        entry["id"]: entry
        for entry in _recipe_engines(
            "media.transcribe",
            {"configured": True, "engines": [advertisement]},
        )
    }
    gateway = {
        target["target"]: target for target in engines["whisper-turbo"]["targets"]
    }["models-gateway"]
    assert gateway["available"] is False
    assert gateway["models"] == []
    assert "frisket.transcription.v1" in gateway["error"]


def test_parakeet_gateway_requires_v1_contract_advertisement():
    advertisement = {
        "route": "/v1/transcribe",
        "name": "parakeet-tdt",
        "available": True,
        "models": ["istupakov/parakeet-tdt-0.6b-v3-onnx"],
        "contract_versions": ["frisket.transcription.v1"],
    }
    engines = {
        entry["id"]: entry
        for entry in _recipe_engines(
            "media.transcribe",
            {"configured": True, "engines": [advertisement]},
        )
    }
    gateway = {
        target["target"]: target for target in engines["parakeet-tdt"]["targets"]
    }["models-gateway"]
    assert gateway["available"] is True
    assert gateway["models"] == ["istupakov/parakeet-tdt-0.6b-v3-onnx"]


def test_moss_v1_advertisement_wins_regardless_of_duplicate_order():
    """An unversioned same-named advertisement must never mask a runnable v1
    worker; availability is advertisement-order independent."""
    unversioned = {
        "route": "/v1/transcribe",
        "name": "moss",
        "available": True,
        "models": [],
    }
    v1 = {
        "route": "/v1/transcribe",
        "name": "moss",
        "available": True,
        "models": ["OpenMOSS-Team/MOSS-Transcribe-Diarize"],
        "contract_versions": ["frisket.transcription.v1"],
    }
    for ordering in ([unversioned, v1], [v1, unversioned]):
        engines = {
            e["id"]: e
            for e in _recipe_engines(
                "media.transcribe", {"configured": True, "engines": list(ordering)}
            )
        }
        assert engines["moss"]["available"] is True, ordering
        assert engines["moss"]["models"] == ["OpenMOSS-Team/MOSS-Transcribe-Diarize"]


def test_every_symbolic_engine_has_an_exhaustive_capability_declaration():
    from frisket.contracts.actions.schemas._engines import TRANSCRIBE_ENGINE_TABLE

    for entry in TRANSCRIBE_ENGINE_TABLE:
        capabilities = transcribe_engine_capabilities(entry.id)
        assert capabilities is entry.transcription
        for alias in entry.aliases:
            assert transcribe_engine_capabilities(alias) is capabilities

    with pytest.raises(ValueError, match="unknown transcription engine"):
        transcribe_engine_capabilities("future-engine-without-a-row")


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"language_mode": "multi"}, "at most one language"),
    ],
)
def test_v1_capability_declarations_reject_unrepresentable_shapes(
    updates,
    message,
):
    from frisket.contracts.actions.schemas._engines import (
        TranscriptionEngineCapabilities,
    )

    values = {
        "transport": "frisket.transcription.v1",
        "language_mode": "single",
        "detects_language": True,
        **updates,
    }
    with pytest.raises(ValueError, match=message):
        TranscriptionEngineCapabilities(**values)


def test_parakeet_engine_advertises_pullable_pinned_artifacts():
    """The parakeet engine hint lists the two pinned hf-snapshot artifacts
    (human name + pinned revision + approximate size) so a pre-download can be
    started via POST /api/providers/models/pull instead of surprising a
    sensitive run with a first-use fetch."""
    from frisket.ai.models import artifact_manifest

    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}
    downloadable = engines["parakeet-tdt"]["downloadable_models"]
    by_name = {d["display_name"]: d for d in downloadable}
    assert set(by_name) == {
        "Parakeet transcription model",
        "Silero voice-activity model",
    }
    model = by_name["Parakeet transcription model"]
    assert model["ref"] == artifact_manifest.PARAKEET_MODEL_REF
    assert model["revision"] in model["ref"]
    assert model["size"] and model["size"] > 100_000_000  # ~660 MB, approximate
    vad = by_name["Silero voice-activity model"]
    assert vad["ref"] == artifact_manifest.PARAKEET_VAD_REF
    assert vad["size"] and vad["size"] < 100_000_000  # ~2 MB, approximate


def test_faster_whisper_engine_advertises_pullable_pinned_base_artifact():
    """faster_whisper's default 'base' size is now the same kind of pinned
    hf-snapshot pull as Parakeet's artifacts (task: complete the
    local-model provisioning story) -- one entry, same shape."""
    from frisket.ai.models import artifact_manifest

    engines = {e["id"]: e for e in _recipe_engines("media.transcribe", {})}
    downloadable = engines["faster_whisper"]["downloadable_models"]
    assert len(downloadable) == 1
    entry = downloadable[0]
    assert entry["ref"] == artifact_manifest.WHISPER_BASE_REF
    assert entry["revision"] in entry["ref"]
    assert entry["license"] == "MIT"
    assert entry["size"] and 100_000_000 < entry["size"] < 200_000_000  # ~145 MB


def test_language_declaration_survives_catalog_projection():
    payload = action_catalog_payload_with_launcher_hints({})
    entry = next(a for a in payload["actions"] if a["kind"] == "media.transcribe")
    engines = {e["id"]: e for e in entry["ui_hints"]["engines"]}
    assert engines["parakeet-tdt"]["language"] == {
        "mode": "auto_only",
        "default": "auto",
        "detects": False,
        "allows_auto": True,
        "choices": None,
        "fixed_language": None,
    }
    assert engines["faster_whisper"]["language"]["detects"] is True


# --------------------------------------------------------------------------- #
# Step 2: wire param + canonicalization + engine-mode enforcement


def _params(**overrides):
    base = {
        "source": "media",
        "engine": "faster_whisper",
    }
    base.update(overrides)
    return base


def test_params_language_canonicalized_to_list():
    # dedupe within the single-mode cardinality limit
    p = TranscribeParams.model_validate(_params(language=["es", "es"]))
    assert p.language == ["es"]
    # auto spellings all collapse to []
    for spelling in (None, [], ["auto"], ["auto", " "]):
        assert (
            TranscribeParams.model_validate(_params(language=spelling)).language == []
        )
    # absent field -> [] (validate_default runs the canonicalizer)
    assert TranscribeParams.model_validate(_params()).language == []


def test_single_engine_rejects_multi_list():
    with pytest.raises(ValidationError) as exc:
        TranscribeParams.model_validate(
            _params(engine="faster_whisper", language=["en", "es"])
        )
    assert "invalid_language_selection" in str(exc.value)


def test_single_engine_rejects_unknown_code():
    with pytest.raises(ValidationError) as exc:
        TranscribeParams.model_validate(
            _params(engine="faster_whisper", language=["zz"])
        )
    assert "invalid_language_selection" in str(exc.value)
    # a real whisper code is accepted
    assert TranscribeParams.model_validate(
        _params(engine="faster_whisper", language=["fr"])
    ).language == ["fr"]


def test_auto_only_engine_canonicalizes_auto_and_rejects_overrides():
    # Auto omits the unsupported caller language hint from actual dispatch;
    # an explicit language remains a loud error.
    assert "language" not in _normalized(engine="parakeet-tdt", language=[])
    assert "language" not in _normalized(engine="parakeet-tdt", language=["auto"])
    for language in (["en"], ["es"]):
        with pytest.raises(ValidationError, match="invalid_language_selection"):
            TranscribeParams.model_validate(
                _params(engine="parakeet-tdt", language=language)
            )


def test_auto_only_engine_absent_and_auto_normalize_identically():
    def options(**overrides):
        return _normalized(engine="parakeet-tdt", **overrides)

    assert options() == options(language=["auto"]) == options(language=[])


def test_unsupported_knobs_fail_closed_on_every_engine():
    # The old permissive tolerance
    # that silently dropped undeclared knobs on legacy engines is gone —
    # EVERY engine, including free-form provider/model ids, rejects an
    # authored knob its declaration does not carry.
    cases = [
        ("openai/whisper-1", {"vad": True}),
        ("openai/whisper-1", {"vad": False}),
        ("openai/whisper-1", {"model_size": "large-v3"}),
        ("parakeet-tdt", {"model_size": "base"}),  # size is Whisper's knob
        ("moss", {"vad": True}),
        ("moss", {"model_size": "base"}),
        ("vibevoice-asr", {"vad": True}),
        ("vibevoice-asr", {"model_size": "base"}),
    ]
    for engine, knobs in cases:
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(_params(engine=engine, **knobs))
        assert "transcription_option_unavailable" in str(exc.value), (engine, knobs)
    with pytest.raises(ValidationError, match="invalid_language_selection"):
        TranscribeParams.model_validate(
            _params(engine="vibevoice-asr", language=["en"])
        )
    # Declared knobs still author fine.
    ok = TranscribeParams.model_validate(
        _params(engine="faster_whisper", vad=False, model_size="base")
    )
    assert ok.vad is False and ok.model_size == "base"


def test_mixed_auto_plus_code_fails_validation_loudly():
    # ["auto","en"] must not silently become ["en"].
    with pytest.raises(ValidationError) as exc:
        TranscribeParams.model_validate(
            _params(engine="faster_whisper", language=["auto", "en"])
        )
    assert "invalid_language_selection" in str(exc.value)


def _normalized(**overrides):
    params = TranscribeParams.model_validate(_params(**overrides))
    return transcribe_options(params).normalize(params.engine.root)


def test_canonical_language_feeds_admitted_options_identity():
    assert _normalized() == _normalized(language=[]) == _normalized(language=["auto"])
    assert _normalized(language=["fr"]) != _normalized()


def test_language_scalar_canonicalizes_before_typed_validation():
    assert _normalized(language="en") == _normalized(language=["en"])


def test_recipe_spec_language_extracts_scalar():
    from frisket.sdk.ops.transcription.common import _spec_language

    assert _spec_language({"language": ["fr"]}) == "fr"
    assert _spec_language({"language": []}) is None
    assert _spec_language({}) is None
    assert _spec_language({"language": "de"}) == "de"  # legacy scalar tolerated


# --------------------------------------------------------------------------- #
# Step 4: detected_language emitted only by detecting engines


def test_detected_language_gated_on_detects():
    def names(engine):
        params = TranscribeParams.model_validate(_params(engine=engine))
        return [field.key for field in TRANSCRIBE.run.resolve_output_fields(params)]

    for engine in ("faster_whisper", "whisper"):
        assert "detected_language" in names(engine)
    for engine in ("parakeet-tdt", "vibevoice-asr"):
        assert names(engine) == ["text", "segments"]


def test_segments_schema_carries_optional_speaker():
    fields = TRANSCRIBE.run.resolve_output_fields(
        TranscribeParams.model_validate(_params())
    )
    seg = next(field for field in fields if field.key == "segments")
    schema = seg.schema
    props = schema["$defs"]["TranscriptSegment"]["properties"]
    assert "speaker" in props and props["speaker"]["type"] == "string"
    # Speaker identity is segment-level, never repeated per word.
    assert "speaker" not in schema["$defs"]["TranscriptWord"]["properties"]


# --------------------------------------------------------------------------- #
# Step 6: diarization params


def test_diarize_true_errors_as_unavailable_on_non_diarizing_engines():
    for engine in ("faster_whisper", "whisper"):
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(_params(engine=engine, diarize=True))
        assert "diarization_unavailable" in str(exc.value)


def test_diarize_true_is_valid_on_parakeet_tdt():
    # Union-of-declared-support: SOME parakeet-tdt target (the gateway) diarizes,
    # so authoring accepts diarize=true on the canonical id. A roster whose
    # able build cannot diarize is a RESOLUTION refusal
    # (no_capable_target, tests/execution/test_one_engine_two_targets.py),
    # never a silent authoring-time drop.
    p = TranscribeParams.model_validate(_params(engine="parakeet-tdt", diarize=True))
    assert p.diarize is True
    assert (p.num_speakers, p.min_speakers, p.max_speakers) == (None, None, None)


def test_speaker_count_hints_rejected_on_speaker_hint_none_engine():
    # Closure-sweep L5: parakeet-tdt declares speaker_hint="none" (offline
    # Sortformer v1 consumes no count hints), so a supplied count knob is a
    # loud validation error on EVERY transport — not accepted, wire-forwarded,
    # and ignored while splitting the params hash.
    for hints in (
        {"num_speakers": 2},
        {"min_speakers": 2, "max_speakers": 4},
        {"num_speakers": 4},
    ):
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(
                _params(engine="parakeet-tdt", diarize=True, **hints)
            )
        assert "transcription_option_unavailable" in str(exc.value)


def test_speaker_hint_above_sortformer_cap_is_rejected():
    # The hard product cap fires first (>4 can never be honoured anywhere);
    # at/below the cap the speaker_hint="none" declaration rejects the knob
    # (test above).
    for hints in (
        {"num_speakers": 5},
        {"min_speakers": 5},
        {"min_speakers": 2, "max_speakers": 6},
    ):
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(
                _params(engine="parakeet-tdt", diarize=True, **hints)
            )
        assert "diarization_speaker_cap" in str(exc.value)


def test_diarize_false_is_the_default_and_valid():
    p = TranscribeParams.model_validate(_params())
    assert p.diarize is False
    assert (p.num_speakers, p.min_speakers, p.max_speakers) == (None, None, None)


def test_mai_default_diarization_and_clean_are_canonical_and_distinct():
    def options(**overrides):
        return _normalized(engine="openrouter/microsoft/mai-transcribe-2", **overrides)

    assert options()["diarize"] is True
    assert options()["clean"] is False
    assert options() == options(diarize=True) == options(clean=False)
    assert options() != options(diarize=False)
    assert options() != options(clean=True)


def test_speaker_hint_requires_diarize():
    with pytest.raises(ValidationError) as exc:
        TranscribeParams.model_validate(_params(num_speakers=2))
    assert "invalid_params" in str(exc.value)


def test_num_speakers_and_range_are_mutually_exclusive():
    # (also needs an engine that supports diarization to get past the
    # unavailable gate — none do, so assert the conflict is caught first is not
    # guaranteed; instead assert the combination is rejected outright)
    with pytest.raises(ValidationError):
        TranscribeParams.model_validate(
            _params(diarize=True, num_speakers=2, min_speakers=1, max_speakers=3)
        )


def test_diarization_params_presence_and_normalized_identity():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        TranscribeParams.model_validate(_params(bogus_param=1))
    assert _normalized(diarize=False) == _normalized()
    assert "diarize" not in TranscribeParams.model_validate(_params()).model_fields_set
    assert (
        "diarize"
        in TranscribeParams.model_validate(_params(diarize=False)).model_fields_set
    )


def test_intrinsic_declaration_removes_diarize_from_identity_and_projection(
    monkeypatch,
):
    from frisket.actions import media_options as media
    from frisket.contracts.actions.schemas._engines import (
        EngineDeclaration,
        TranscriptionEngineCapabilities,
    )

    engine = "synthetic-intrinsic-v1"
    declaration = EngineDeclaration(
        id=engine,
        label="Synthetic intrinsic engine",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="single",
            detects_language=True,
            diarization_mode="intrinsic",
            speaker_hint="none",
        ),
    )
    monkeypatch.setattr(
        media,
        "TRANSCRIBE_ENGINE_TABLE",
        (*media.TRANSCRIBE_ENGINE_TABLE, declaration),
    )

    assert "diarize" not in TranscriptionOptions().normalize(engine)
    for unsupported in (
        {"diarize": False},
        {"diarize": True},
        {"vad": False},
        {"model_size": "large-v3"},
    ):
        with pytest.raises(ValueError, match="transcription_option_unavailable"):
            TranscriptionOptions.model_validate(unsupported).normalize(engine)

    assert TranscriptionOptions(language=["en"]).normalize(engine) == {
        "language": ["en"]
    }


def test_v1_optional_engine_without_speaker_hint_rejects_count(monkeypatch):
    from frisket.actions import media_options as media
    from frisket.contracts.actions.schemas._engines import (
        EngineDeclaration,
        TranscriptionEngineCapabilities,
    )

    engine = "synthetic-optional-v1"
    declaration = EngineDeclaration(
        id=engine,
        label="Synthetic optional v1 engine",
        tier="sidecar",
        provider="frisket-sidecar",
        transcription=TranscriptionEngineCapabilities(
            transport="frisket.transcription.v1",
            language_mode="auto_only",
            detects_language=True,
            vad=True,
            diarization_mode="optional",
            speaker_hint="none",
            max_speakers=4,
        ),
    )
    monkeypatch.setattr(
        media,
        "TRANSCRIBE_ENGINE_TABLE",
        (*media.TRANSCRIBE_ENGINE_TABLE, declaration),
    )

    assert TranscriptionOptions(diarize=True).normalize(engine)["diarize"] is True
    with pytest.raises(ValueError, match="transcription_option_unavailable"):
        TranscriptionOptions(diarize=True, num_speakers=2).normalize(engine)

    absent_vad = TranscriptionOptions().normalize(engine)
    explicit_true = TranscriptionOptions(vad=True).normalize(engine)
    explicit_false = TranscriptionOptions(vad=False).normalize(engine)
    assert "vad" not in absent_vad
    assert explicit_true["vad"] is True
    assert explicit_false["vad"] is False
    assert absent_vad != explicit_true
    assert absent_vad != explicit_false
    assert explicit_true != explicit_false


# --------------------------------------------------------------------------- #
# `context` (hotwords) — declaration-gated on local and v1 Faster Whisper


def test_context_capability_is_coherent_with_supported_transports():
    from frisket.contracts.actions.schemas._engines import (
        TranscriptionEngineCapabilities,
    )

    # A v1 engine may declare the hotword prompt...
    capabilities = TranscriptionEngineCapabilities(
        transport="frisket.transcription.v1",
        language_mode="auto_only",
        detects_language=False,
        context=True,
    )
    assert capabilities.context is True
    local = TranscriptionEngineCapabilities(
        transport="local",
        language_mode="single",
        detects_language=True,
        context=True,
    )
    assert local.context is True
    # A declared remote profile may map it to its provider-specific options.
    remote = TranscriptionEngineCapabilities(
        transport="remote",
        language_mode="single",
        detects_language=True,
        context=True,
    )
    assert remote.context is True


def test_transcription_context_support_matches_engine_capabilities():
    assert transcribe_engine_capabilities("moss").context is True
    assert transcribe_engine_capabilities("vibevoice-asr").context is True
    assert transcribe_engine_capabilities("faster_whisper").context is True
    for engine in (
        "parakeet-tdt",
        "openai/whisper-1",  # free-form remote ids fail closed too
    ):
        assert transcribe_engine_capabilities(engine).context is False


def test_context_param_accepted_only_on_declaring_engines():
    for engine in ("moss", "vibevoice-asr", "faster_whisper"):
        validated = TranscribeParams.model_validate(
            _params(engine=engine, context="  Frisket, Sortformer  ")
        )
        assert validated.context == "Frisket, Sortformer"
    for engine in (
        "parakeet-tdt",
        "openai/whisper-1",
    ):
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(_params(engine=engine, context="Frisket"))
        assert "transcription_option_unavailable" in str(exc.value)
    # Even an explicit-null spelling is loud on a non-declaring engine —
    # accepting it would create false option provenance.
    with pytest.raises(ValidationError) as exc:
        TranscribeParams.model_validate(_params(engine="parakeet-tdt", context=None))
    assert "transcription_option_unavailable" in str(exc.value)


def test_context_param_constraints():
    from frisket.contracts.transcription_sidecar import TRANSCRIPTION_CONTEXT_MAX_CHARS

    for bad in ("", "   ", "x" * (TRANSCRIPTION_CONTEXT_MAX_CHARS + 1)):
        with pytest.raises(ValidationError) as exc:
            TranscribeParams.model_validate(_params(engine="moss", context=bad))
        assert "invalid_params" in str(exc.value)
    at_cap = "x" * TRANSCRIPTION_CONTEXT_MAX_CHARS
    assert (
        TranscribeParams.model_validate(_params(engine="moss", context=at_cap)).context
        == at_cap
    )


def test_context_normalized_identity_absent_null_unify_and_supplied_differs():
    def options(**overrides):
        return _normalized(engine="moss", **overrides)

    assert options() == options(context=None)
    assert options(context="Frisket") != options()
    assert options(context="Frisket") == options(context="  Frisket  ")
    assert "context" not in options(context=None)


def test_context_projected_only_for_declaring_engines():
    from frisket.contracts.action import project_transcription_engine_options

    assert project_transcription_engine_options(
        "moss",
        {"context": "Frisket, Sortformer", "vad": True, "language": ["en"]},
    ) == {"context": "Frisket, Sortformer"}
    assert project_transcription_engine_options(
        "faster_whisper", {"context": "Frisket"}
    ) == {"context": "Frisket"}
    # A non-declaring engine never sees the knob even if a stale spec carries it.
    projected = project_transcription_engine_options(
        "parakeet-tdt", {"context": "Frisket"}
    )
    assert "context" not in projected
