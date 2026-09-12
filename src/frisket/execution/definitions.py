"""Static, code-owned execution-target definitions.

Target rows declare each engine's transport, option support, sizes, and
run-scoped behavior:

- ``local``           — ``faster_whisper`` (sandboxed-process placement with
  a strict ``frisket.transcription.v1`` subprocess payload; the sizes the
  local CPU worker serves); always live.
- ``local-onnx``      — ``parakeet-tdt`` (transport ``local``; run-scoped;
  NO diarization — the local ONNX build has no diarizer); live iff the
  pinned Parakeet + Silero-VAD snapshots are present in the Hub cache
  (artifact-manifest probe, read from disk at every probe) — absence is a
  ``no_live_target`` refusal with the download remedy, never a dispatch-time
  surprise.
- ``models-gateway``  — the fixed named ``whisper-turbo``, ``parakeet-tdt``,
  ``moss``, and ``vibevoice-asr`` models over ``frisket.transcription.v1``;
  live iff
  FRISKET_MODELS_URL + TOKEN. The hosted Parakeet worker supports optional
  diarization; the local ONNX build remains flat transcription only.
- ``remote-api:{provider}`` — minted per provider the model router knows
  (``ai/llm/router.py``'s adapter roster); serves free-form
  ``<provider>/<model>`` engines, declared here as the ``{provider}/*``
  wildcard support entry (e.g. ``openai/whisper-1`` via
  ``remote-api:openai``).
- ``datalab`` — the hosted OCR API: a third-party document venue billed
  per PAGE, live iff a Datalab key resolves (env or project secret).

**A target is a VENUE, not a kind of work.** Each ``TargetEngineSupport``
row declares the CAPABILITY it serves, so the same rows above carry OCR too:
``local`` gains rapidocr/tesseract, ``models-gateway`` gains dots.mocr,
GLM-OCR, Surya 2, and paddleocr-vl over its own ``sidecar.ocr`` wire, and every
``remote-api`` target gains an OCR ``{provider}/*`` row for vision-model OCR.
Only Datalab needed a new venue. Resolution filters candidates by capability
first, so two capabilities can share a venue (and even an engine SPELLING)
without ever seeing each other's rows.

Which target serves a given invocation is the resolver's §10 preference
table (``resolver.py``): raw authored symbols pin their historical target;
fresh canonical names take the FIRST ABLE target in the declaration order
below (ability-driven only — e.g. diarize -> models-gateway, the only
diarizing Parakeet build; never liveness-driven). An authored local Whisper size beyond the
local set refuses (``no_capable_target``). The separately named
``whisper-turbo`` engine has no size selection.

**Environment-read discipline:** this module is the
ONE app-side place placement/activation env names appear. The existing env
names ARE the activation inputs — no ``FRISKET_TARGET_*`` aliases are
introduced. Each name is documented at its constant below; adapters receive
them through ``ConnectionConfig`` instead of reading the environment directly.
"""

from __future__ import annotations

import math
import os
from importlib.util import find_spec
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from frisket.contracts.actions.schemas._engines import (
    OCR_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    TranscriptionEngineCapabilities,
    find_engine,
)
from frisket.contracts.transcription_sidecar import TranscriptionOptionSupport
from frisket.execution.provider import ConnectionConfig
from frisket.execution.targets import (
    CAPABILITY_CENSUS as CAPABILITY_CENSUS,
    CAPABILITY_GEOCODE as CAPABILITY_GEOCODE,
    CAPABILITY_OCR as CAPABILITY_OCR,
    CAPABILITY_TO_MARKDOWN as CAPABILITY_TO_MARKDOWN,
    CAPABILITY_TRANSCRIBE as CAPABILITY_TRANSCRIBE,
    CAPABILITY_TRANSLATE as CAPABILITY_TRANSLATE,
    DATALAB_TARGET_ID as DATALAB_TARGET_ID,
    DEEPL_TARGET_ID as DEEPL_TARGET_ID,
    GOOGLE_TRANSLATE_TARGET_ID as GOOGLE_TRANSLATE_TARGET_ID,
    NOMINATIM_TARGET_ID as NOMINATIM_TARGET_ID,
    OPENCAGE_TARGET_ID as OPENCAGE_TARGET_ID,
    REMOTE_API_TARGET_ID_PREFIX as REMOTE_API_TARGET_ID_PREFIX,
    US_CENSUS_TARGET_ID as US_CENSUS_TARGET_ID,
    CensusOptionSupport,
    ExecutionTarget,
    GeocodeOptionSupport,
    OcrOptionSupport,
    TargetEngineSupport,
    ToMarkdownOptionSupport,
    TranslateOptionSupport,
)

MODELS_GATEWAY_URL_ENV = "FRISKET_MODELS_URL"
MODELS_GATEWAY_TOKEN_ENV = "FRISKET_MODELS_TOKEN"
MODELS_GATEWAY_TIMEOUT_ENV = "FRISKET_TRANSCRIPTION_SIDECAR_TIMEOUT_SECONDS"
DEFAULT_MODELS_GATEWAY_TIMEOUT_SECONDS = 3600.0
MODELS_GATEWAY_CONNECT_TIMEOUT_SECONDS = 10.0

ROUTER_PROVIDER_KEY_ENV: Mapping[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


DATALAB_API_KEY_ENV = "DATALAB_API_KEY"

DEEPL_API_KEY_ENV = "DEEPL_API_KEY"
GOOGLE_TRANSLATE_API_KEY_ENV = "GOOGLE_TRANSLATE_API_KEY"
OPENCAGE_API_KEY_ENV = "OPENCAGE_API_KEY"
CENSUS_API_KEY_ENV = "CENSUS_API_KEY"

LOCAL_TARGET_ID = "local"
LOCAL_ONNX_TARGET_ID = "local-onnx"
MODELS_GATEWAY_TARGET_ID = "models-gateway"


def _hf_snapshot_present(repo_id: str, revision: str, files: Sequence[str]) -> bool:
    """Probe the pinned Hub snapshot layout without network access."""
    from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

    snapshot: Path = (
        huggingface_hub_cache()
        / f"models--{repo_id.replace('/', '--')}"
        / "snapshots"
        / revision
    )
    return all((snapshot / name).is_file() for name in files)


def parakeet_artifacts_present() -> bool:
    """Return whether both pinned Parakeet snapshots are installed.

    Read disk on every call because downloads occur in another process.
    """
    from frisket.ai.models import artifact_manifest

    for entry in (
        artifact_manifest.parakeet_model_artifact(),
        artifact_manifest.parakeet_vad_artifact(),
    ):
        if entry is None or entry.hf_snapshot is None:
            return False
        snap = entry.hf_snapshot
        if not _hf_snapshot_present(snap.repo_id, snap.revision, snap.files):
            return False
    return True


def faster_whisper_runtime_present() -> bool:
    """Return whether the optional local Whisper runtime is installed."""
    try:
        return find_spec("faster_whisper") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def parakeet_runtime_present() -> bool:
    """Return whether every module owned by the ``asr`` extra is installed."""
    try:
        return all(
            find_spec(module) is not None
            for module in ("onnx_asr", "onnxruntime", "huggingface_hub", "av")
        )
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _mirror_options(
    capabilities: TranscriptionEngineCapabilities,
) -> TranscriptionOptionSupport:
    """Mirror an engine declaration's option support."""
    return TranscriptionOptionSupport.model_validate(
        {
            "diarization_mode": capabilities.diarization_mode,
            "speaker_hint": capabilities.speaker_hint,
            "language": capabilities.accepts_language,
            "model_size": capabilities.model_size,
            "vad": capabilities.vad,
            "context": capabilities.context,
            "clean": capabilities.clean,
        }
    )


def _engine_support(engine_id: str) -> TargetEngineSupport:
    """Mirror roster support for an engine with one serving target."""
    entry = find_engine(TRANSCRIBE_ENGINE_TABLE, engine_id)
    assert entry is not None and entry.transcription is not None
    return TargetEngineSupport(
        engine=entry.id,
        transport=entry.transcription.transport,
        capability=CAPABILITY_TRANSCRIBE,
        options=_mirror_options(entry.transcription),
        run_scoped=entry.run_scoped,
    )


def _option_support(
    *,
    language: bool,
    vad: bool,
    diarization_mode: str = "none",
    speaker_hint: str = "none",
    model_size: bool = False,
    context: bool = False,
    clean: bool = False,
) -> TranscriptionOptionSupport:
    """Declare options for one target of a multi-target engine."""
    return TranscriptionOptionSupport.model_validate(
        {
            "diarization_mode": diarization_mode,
            "speaker_hint": speaker_hint,
            "language": language,
            "model_size": model_size,
            "vad": vad,
            "context": context,
            "clean": clean,
        }
    )


LOCAL_WHISPER_SIZES: tuple[str, ...] = ("tiny", "base", "small", "medium")


def _ocr_support(
    engine: str,
    transport: str,
    *,
    language: bool,
    geometry: bool,
    run_scoped: bool = False,
) -> TargetEngineSupport:
    return TargetEngineSupport(
        engine=engine,
        transport=transport,  # type: ignore[arg-type]
        capability=CAPABILITY_OCR,
        options=OcrOptionSupport(language=language, geometry=geometry),
        run_scoped=run_scoped,
    )


def _local_ocr_engines() -> tuple[TargetEngineSupport, ...]:
    """Build local OCR support using roster-owned resource scope."""
    return (
        _ocr_support(
            "rapidocr",
            "local",
            language=True,
            geometry=True,
            run_scoped=_roster_run_scoped("rapidocr"),
        ),
        _ocr_support(
            "tesseract",
            "local",
            language=True,
            geometry=True,
            run_scoped=_roster_run_scoped("tesseract"),
        ),
    )


def _roster_run_scoped(engine_id: str) -> bool:
    entry = find_engine(OCR_ENGINE_TABLE, engine_id)
    assert entry is not None, engine_id
    return entry.run_scoped


def _gateway_ocr_engines() -> tuple[TargetEngineSupport, ...]:
    """The gateway's OCR engines over POST /ocr. No language knob: the
    settled contract carries pages + engine only."""
    return (
        _ocr_support("dots.mocr", "sidecar.ocr", language=False, geometry=True),
        _ocr_support("glm-ocr", "sidecar.ocr", language=False, geometry=False),
        _ocr_support("surya2", "sidecar.ocr", language=False, geometry=True),
        _ocr_support("pp-ocrv6", "sidecar.ocr", language=False, geometry=True),
        _ocr_support("paddleocr-vl", "sidecar.ocr", language=False, geometry=True),
    )


def _remote_ocr_wildcard_support(provider: str) -> TargetEngineSupport:
    """The ``{provider}/*`` OCR row: a router-served VLM reads the page as an
    image part. It takes a language HINT (it rides the prompt) and returns no
    geometry at all — so ``searchable_pdf`` refuses here rather than painting
    an empty text layer."""
    return _ocr_support(f"{provider}/*", "remote", language=True, geometry=False)


def _remote_wildcard_support(provider: str) -> TargetEngineSupport:
    """The ``{provider}/*`` wildcard row: free-form ``provider/model`` engines
    share the explicit remote transport family (one language hint accepted;
    everything else unsupported), matching
    ``transcription_engine_capabilities``' provider/model branch."""
    return TargetEngineSupport(
        engine=f"{provider}/*",
        transport="remote",
        capability=CAPABILITY_TRANSCRIBE,
        options=TranscriptionOptionSupport.model_validate(
            {
                "diarization_mode": "none",
                "speaker_hint": "none",
                "language": True,
                "model_size": False,
                "vad": False,
                "context": False,
                "clean": False,
            }
        ),
    )


def _translate_support(
    engine: str,
    transport: str,
    *,
    source_language: bool,
    auto_detect_source: bool,
) -> TargetEngineSupport:
    return TargetEngineSupport(
        engine=engine,
        transport=transport,  # type: ignore[arg-type]
        capability=CAPABILITY_TRANSLATE,
        options=TranslateOptionSupport(
            source_language=source_language,
            auto_detect_source=auto_detect_source,
        ),
    )


def _to_markdown_support(engine: str, transport: str) -> TargetEngineSupport:
    return TargetEngineSupport(
        engine=engine,
        transport=transport,  # type: ignore[arg-type]
        capability=CAPABILITY_TO_MARKDOWN,
        options=ToMarkdownOptionSupport(),
    )


def _geocode_support(
    engine: str, transport: str, *, structured_query: bool
) -> TargetEngineSupport:
    return TargetEngineSupport(
        engine=engine,
        transport=transport,  # type: ignore[arg-type]
        capability=CAPABILITY_GEOCODE,
        options=GeocodeOptionSupport(structured_query=structured_query),
    )


def _local_translate_engines() -> tuple[TargetEngineSupport, ...]:
    """Build local MT support with each engine's source-language semantics."""
    return (
        _translate_support(
            "opus_mt", "local", source_language=True, auto_detect_source=False
        ),
        _translate_support(
            "hy_mt2", "local", source_language=False, auto_detect_source=True
        ),
    )


def _local_to_markdown_engines() -> tuple[TargetEngineSupport, ...]:
    """Build local converters, including resolvable unlisted engines."""
    return (
        _to_markdown_support("markitdown", "local"),
        _to_markdown_support("trafilatura_html", "local"),
    )


def _gateway_to_markdown_engines() -> tuple[TargetEngineSupport, ...]:
    """Build gateway converters served by ``POST /to-markdown``."""
    return (
        _to_markdown_support("docling", "sidecar.convert"),
        _to_markdown_support("chandra", "sidecar.convert"),
    )


def build_static_targets() -> tuple[ExecutionTarget, ...]:
    """Build code-owned targets in preference-free declaration order."""
    return (
        ExecutionTarget(
            id=LOCAL_TARGET_ID,
            operator="self",
            egress_class="none",
            engines=(
                TargetEngineSupport(
                    engine="faster_whisper",
                    # ``local`` selects the sandboxed subprocess placement;
                    # the parent/worker payload itself is the same strict
                    # frisket.transcription.v1 contract used by the gateway.
                    transport="local",
                    capability=CAPABILITY_TRANSCRIBE,
                    options=_option_support(
                        language=True,
                        model_size=True,
                        vad=True,
                        context=True,
                    ),
                    sizes=LOCAL_WHISPER_SIZES,
                ),
                *_local_ocr_engines(),
                *_local_translate_engines(),
                *_local_to_markdown_engines(),
            ),
        ),
        ExecutionTarget(
            id=LOCAL_ONNX_TARGET_ID,
            operator="self",
            egress_class="none",
            engines=(
                TargetEngineSupport(
                    engine="parakeet-tdt",
                    transport="local",
                    capability=CAPABILITY_TRANSCRIBE,
                    options=_option_support(language=False, vad=True),
                    run_scoped=True,
                ),
            ),
        ),
        ExecutionTarget(
            id=MODELS_GATEWAY_TARGET_ID,
            operator="self",
            egress_class="operator_lan",
            engines=(
                TargetEngineSupport(
                    engine="whisper-turbo",
                    transport="frisket.transcription.v1",
                    capability=CAPABILITY_TRANSCRIBE,
                    options=_option_support(
                        language=True,
                        vad=True,
                        context=True,
                    ),
                ),
                TargetEngineSupport(
                    engine="parakeet-tdt",
                    transport="frisket.transcription.v1",
                    capability=CAPABILITY_TRANSCRIBE,
                    options=_option_support(
                        language=False, vad=True, diarization_mode="optional"
                    ),
                ),
                _engine_support("moss"),
                _engine_support("vibevoice-asr"),
                *_gateway_ocr_engines(),
                *_gateway_to_markdown_engines(),
            ),
        ),
        ExecutionTarget(
            id=DATALAB_TARGET_ID,
            operator="datalab",
            egress_class="third_party_api",
            engines=(
                _ocr_support(
                    "datalab", "datalab.convert", language=False, geometry=True
                ),
                _to_markdown_support("datalab", "datalab.convert"),
            ),
        ),
        ExecutionTarget(
            id=DEEPL_TARGET_ID,
            operator="deepl",
            egress_class="third_party_api",
            engines=(
                _translate_support(
                    "deepl",
                    "deepl.v2",
                    source_language=True,
                    auto_detect_source=True,
                ),
            ),
        ),
        ExecutionTarget(
            id=GOOGLE_TRANSLATE_TARGET_ID,
            operator="google",
            egress_class="third_party_api",
            engines=(
                _translate_support(
                    "google_translate",
                    "google.translate.v2",
                    source_language=True,
                    auto_detect_source=True,
                ),
            ),
        ),
        ExecutionTarget(
            id=OPENCAGE_TARGET_ID,
            operator="opencage",
            egress_class="third_party_api",
            engines=(
                _geocode_support("opencage", "opencage.v1", structured_query=True),
            ),
        ),
        ExecutionTarget(
            id=NOMINATIM_TARGET_ID,
            operator="nominatim",
            egress_class="third_party_api",
            engines=(
                _geocode_support(
                    "nominatim", "nominatim.search", structured_query=False
                ),
            ),
        ),
        ExecutionTarget(
            id=US_CENSUS_TARGET_ID,
            operator="us-census",
            egress_class="third_party_api",
            engines=(
                TargetEngineSupport(
                    engine="us_census_acs",
                    transport="census.acs5",
                    capability=CAPABILITY_CENSUS,
                    options=CensusOptionSupport(),
                ),
            ),
        ),
        *(
            ExecutionTarget(
                id=REMOTE_API_TARGET_ID_PREFIX + provider,
                operator=provider,
                egress_class="third_party_api",
                engines=(
                    _remote_wildcard_support(provider),
                    *(
                        _engine_support(entry.id)
                        for entry in TRANSCRIBE_ENGINE_TABLE
                        if entry.provider == provider
                    ),
                    _remote_ocr_wildcard_support(provider),
                ),
            )
            for provider in ROUTER_PROVIDER_KEY_ENV
        ),
    )


# Keyless Nominatim and optionally keyed Census are intentionally absent.
_API_KEY_VENUES: dict[str, tuple[str, str]] = {
    DEEPL_TARGET_ID: (DEEPL_API_KEY_ENV, "deepl"),
    GOOGLE_TRANSLATE_TARGET_ID: (GOOGLE_TRANSLATE_API_KEY_ENV, "google"),
    OPENCAGE_TARGET_ID: (OPENCAGE_API_KEY_ENV, "opencage"),
}

_API_KEY_VENUE_WORK: dict[str, str] = {
    DEEPL_TARGET_ID: "DeepL translation",
    GOOGLE_TRANSLATE_TARGET_ID: "Google Cloud Translation",
    OPENCAGE_TARGET_ID: "OpenCage geocoding",
}


_REMOTE_API_WORK: dict[str, str] = {
    CAPABILITY_TRANSCRIBE: "remote transcription",
    CAPABILITY_OCR: "remote vision-model OCR",
}


class StaticExecutionTargetProvider:
    """Provide code-owned targets with on-demand environment activation."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        secrets: Any | None = None,
        router: Any | None = None,
    ) -> None:
        self._env_override = env
        self._secrets = secrets
        # Use the request's effective key overlay when composition provides it.
        self._router = router
        self._targets = build_static_targets()
        self._by_id = {target.id: target for target in self._targets}
        self.probe_counts: dict[str, int] = {}

    def targets(self) -> Sequence[ExecutionTarget]:
        return self._targets

    def connection(self, target_id: str) -> ConnectionConfig | None:
        self.probe_counts[target_id] = self.probe_counts.get(target_id, 0) + 1
        if target_id not in self._by_id:
            return None
        if target_id == LOCAL_TARGET_ID:
            return ConnectionConfig()
        if target_id == LOCAL_ONNX_TARGET_ID:
            return (
                ConnectionConfig()
                if parakeet_runtime_present() and parakeet_artifacts_present()
                else None
            )
        if target_id == MODELS_GATEWAY_TARGET_ID:
            return self._models_gateway_connection()
        if target_id == DATALAB_TARGET_ID:
            return self._datalab_connection()
        if target_id == NOMINATIM_TARGET_ID:
            return ConnectionConfig(extra={"provider": "nominatim"})
        if target_id == US_CENSUS_TARGET_ID:
            return self._census_connection()
        probe = _API_KEY_VENUES.get(target_id)
        if probe is not None:
            env_name, provider = probe
            return self._api_key_connection(env_name, provider)
        if target_id.startswith(REMOTE_API_TARGET_ID_PREFIX):
            provider = target_id[len(REMOTE_API_TARGET_ID_PREFIX) :]
            return self._remote_api_connection(provider)
        return None

    def liveness_remedy(
        self, target_id: str, capability: str | None = None
    ) -> str | None:
        if target_id == LOCAL_ONNX_TARGET_ID:
            if not parakeet_runtime_present():
                return (
                    "Install Frisket's standard tier in the environment that "
                    "runs the app (pip install 'frisket-data[standard]'), then "
                    "restart Frisket."
                )
            return (
                "Parakeet's pinned ONNX artifacts are not installed. Download "
                "them before retrying."
            )
        if target_id == MODELS_GATEWAY_TARGET_ID:
            return (
                f"Set {MODELS_GATEWAY_URL_ENV} (compose profile 'models') and "
                f"{MODELS_GATEWAY_TOKEN_ENV} to the gateway bearer secret to "
                "enable models-gateway engines."
            )
        if target_id == DATALAB_TARGET_ID:
            return (
                f"Set {DATALAB_API_KEY_ENV} (or add it under Settings > "
                "Secrets) to enable hosted Datalab OCR."
            )
        if target_id in _API_KEY_VENUES:
            env_name, _provider = _API_KEY_VENUES[target_id]
            work = _API_KEY_VENUE_WORK[target_id]
            return (
                f"Set {env_name} (or add it under Settings > Secrets) to enable {work}."
            )
        if target_id.startswith(REMOTE_API_TARGET_ID_PREFIX):
            provider = target_id[len(REMOTE_API_TARGET_ID_PREFIX) :]
            key_env = ROUTER_PROVIDER_KEY_ENV.get(provider)
            if key_env:
                return (
                    f"Set {key_env} (or add a {provider} key under Settings > "
                    f"AI Providers) to enable {provider} "
                    f"{_REMOTE_API_WORK.get(capability or '', 'remote engines')}."
                )
        return None

    def _get(self, name: str) -> str | None:
        environ = os.environ if self._env_override is None else self._env_override
        value = (environ.get(name) or "").strip()
        return value or None

    def _float(self, name: str, default: float) -> float:
        raw = self._get(name)
        if raw is None:
            return default
        try:
            value = float(raw)
        except ValueError:
            raise ValueError(
                f"{name} must be a positive number of seconds, got {raw!r}"
            ) from None
        # NaN and infinity must not reach httpx as deadlines.
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"{name} must be a positive number of seconds, got {raw!r}"
            )
        return value

    def _models_gateway_connection(self) -> ConnectionConfig | None:
        base_url = self._get(MODELS_GATEWAY_URL_ENV)
        token = self._get(MODELS_GATEWAY_TOKEN_ENV)
        if not base_url or not token:
            return None
        return ConnectionConfig(
            base_url=base_url,
            token=token,
            timeout_seconds=self._float(
                MODELS_GATEWAY_TIMEOUT_ENV, DEFAULT_MODELS_GATEWAY_TIMEOUT_SECONDS
            ),
            connect_timeout_seconds=MODELS_GATEWAY_CONNECT_TIMEOUT_SECONDS,
        )

    def _datalab_connection(self) -> ConnectionConfig | None:
        """Probe Datalab using dispatch's environment-then-project order."""
        key = self._get(DATALAB_API_KEY_ENV) or self._project_secret(
            DATALAB_API_KEY_ENV
        )
        if not key:
            return None
        return ConnectionConfig(token=key, extra={"provider": "datalab"})

    def _api_key_connection(
        self, env_name: str, provider: str
    ) -> ConnectionConfig | None:
        """Probe a single-key venue using dispatch's credential order."""
        key = self._get(env_name) or self._project_secret(env_name)
        if not key:
            return None
        return ConnectionConfig(token=key, extra={"provider": provider})

    def _census_connection(self) -> ConnectionConfig:
        """Return an anonymous or optionally keyed Census connection."""
        key = self._get(CENSUS_API_KEY_ENV) or self._project_secret(CENSUS_API_KEY_ENV)
        return ConnectionConfig(token=key, extra={"provider": "us_census_acs"})

    def _project_secret(self, name: str) -> str | None:
        reader = getattr(self._secrets, "secret_plaintext", None)
        if not callable(reader):
            return None
        try:
            value = reader(name)
        except Exception:  # noqa: BLE001 - a broken secret store is not a key
            return None
        return str(value).strip() or None if value else None

    def _project_provider_key(self, provider: str) -> str | None:
        """Read a project key through the same seam as the model router.

        Decryption errors intentionally propagate instead of appearing as a
        misleading missing-key condition.
        """
        reader = getattr(self._secrets, "provider_model_keys", None)
        if not callable(reader):
            return None
        value = reader().get(provider)
        return str(value).strip() or None if value else None

    def _remote_api_connection(self, provider: str) -> ConnectionConfig | None:
        key_env = ROUTER_PROVIDER_KEY_ENV.get(provider)
        if key_env is None:
            return None
        # Match router precedence; generic project secrets are not model keys.
        configured = getattr(self._router, "configured_keys", None)
        effective_keys = configured() if callable(configured) else {}
        key = (
            effective_keys.get(provider)
            if isinstance(effective_keys, Mapping)
            else None
        )
        if not key:
            key = self._project_provider_key(provider) or self._get(key_env)
        if not key:
            return None
        # Connection state carries no rate; pricing requires the chosen engine.
        return ConnectionConfig(token=key, extra={"provider": provider})
