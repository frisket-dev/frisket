"""Hosted OpenAI-dialect transcription adapters, including MAI."""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from frisket.ai.llm.pricing import audio_price
from frisket.ai.llm.types import LLMError
from frisket.ai.models.metadata import ModelCallMeta, PROVIDER_KIND
from frisket.contracts.action import (
    project_transcription_engine_options,
    transcribe_engine_capabilities,
)
from frisket.contracts.actions.schemas._engines import (
    TRANSCRIBE_ENGINE_TABLE,
    TranscriptionEngineCapabilities,
    engine_ids,
)
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseContext,
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.local_model_ids import bare_model_name
from frisket.ops.base import OpContext, RecipeInvocationHalt, media_upload_filename
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.redaction import redact_text

from .common import (
    LocalTranscribeUnsupportedError,
    _segment_with_speaker,
    _spec_language,
    provider_model_call,
    segments_with_index,
)

TranscriptionRequestBuilder = Callable[
    [dict[str, Any], str, str, str, dict[str, Any], TranscriptionEngineCapabilities],
    dict[str, Any],
]


class _RemoteTranscriptionResponseError(HostedEngineError, RuntimeError):
    """A paid response that could not produce a usable transcript."""


def _mai_audio_format(filename: str, content_type: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"wav", "mp3", "flac"}:
        return suffix
    raise ValueError(
        "MAI-Transcribe 2 accepts WAV, MP3, or FLAC audio; "
        f"received {content_type or 'an unknown format'}"
    )


def _mai_transcription_request(
    data: dict[str, Any],
    path: str,
    filename: str,
    content_type: str,
    spec: dict[str, Any],
    capabilities: TranscriptionEngineCapabilities,
) -> dict[str, Any]:
    provider_options: dict[str, Any] = {}
    if capabilities.diarization_mode == "optional":
        provider_options["diarization"] = {
            "enabled": bool(spec.get("diarize", capabilities.diarization_default))
        }
    if capabilities.clean:
        provider_options["enhancedMode"] = {
            "modelOptions": {
                "transcribeStyle": "clean" if spec.get("clean") else "verbatim"
            }
        }
    context = spec.get("context")
    if capabilities.context and isinstance(context, str):
        phrases = [item.strip() for item in context.split(",") if item.strip()]
        if phrases:
            provider_options["phraseList"] = {"phrases": phrases}
    return {
        **data,
        "response_format": "verbose_json",
        "input_audio": {
            "data": base64.b64encode(Path(path).read_bytes()).decode("ascii"),
            "format": _mai_audio_format(filename, content_type),
        },
        "timestamp_granularities": ["segment"],
        "provider": {"options": {"azure": provider_options}},
    }


_TRANSCRIPTION_REQUEST_BUILDERS: dict[str, TranscriptionRequestBuilder] = {
    "openrouter/microsoft/mai-transcribe-2": _mai_transcription_request,
}


def _safe_remote_exception(prefix: str, error: BaseException, key: str) -> str:
    cause_detail = redact_text(error, secret_values=(key,), max_chars=300)
    return redact_text(f"{prefix}{cause_detail}", secret_values=(key,), max_chars=300)


def _remote_response_accounting(
    *,
    model_id: str,
    provider: str,
    credential_source: str,
    ctx: OpContext,
    response: httpx.Response,
    warning: str,
) -> dict[str, Any]:
    price = audio_price(model_id)
    known_zero = price is not None and all(
        isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate == 0
        for rate in price.values()
    )
    provider_cost = 0.0 if known_zero else None
    call = ModelCallMeta.provider_call(
        capability="transcribe",
        engine=model_id,
        provider=provider,
        provider_kind=PROVIDER_KIND.get(provider, "platform_api"),
        model_ids=[bare_model_name(model_id)],
        credential_source=credential_source,
        provider_reported_cost_usd=None,
        provider_cost_usd=provider_cost,
        cost_source=(
            "free_local"
            if known_zero and provider == "ollama"
            else "pricing_data"
            if known_zero
            else "unknown"
        ),
        units={"requests": 1},
        request_id=response.headers.get("x-request-id")
        or response.headers.get("request-id"),
        warnings=[] if known_zero else [warning],
        duration_ms=None,
    ).as_dict()
    admission = routed_admission_in_scope(ctx.extras)
    if admission is not None:
        call = bind_fact_to_route(admission.route, call)
    return {"cost": provider_cost, "model_calls": [call]}


def _halt_unless_consented_credential(
    route: Any, selected_source: Any, effect: str, *, context: CredentialUseContext
) -> None:
    try:
        require_consented_credential(
            cost_posture=route.cost_posture,
            selected_source=selected_source,
            context=context,
            effect=effect,
        )
    except CredentialUseRefusal as exc:
        raise RecipeInvocationHalt(
            "promise_violation", f"{exc}; review the claims and re-consent to resume"
        ) from exc


class OpenAITranscriptionAdapter:
    async def transcribe(
        self, model_id: str, path: str, spec: dict, ctx: OpContext, media: Any
    ) -> dict:
        spec = project_transcription_engine_options(model_id, spec)
        provider, _, model = model_id.partition("/")
        router = (ctx.extras or {}).get("router")
        adapter = (
            router.adapter_for(provider)
            if router is not None and provider != "ollama"
            else None
        )
        local_endpoint = None
        if provider == "ollama" and router is not None:
            try:
                local_endpoint, adapter, model = router.resolve_local_model(model_id)
            except LLMError as exc:
                raise RuntimeError(str(exc)) from exc
        base = getattr(adapter, "base_url", None)
        key = getattr(adapter, "api_key", None)
        if not base or not key:
            raise RuntimeError(
                f"remote transcription needs a configured '{provider}' "
                f"provider (set {provider.upper()}_API_KEY or an org key) — "
                f"or use a local engine: {', '.join(engine_ids(TRANSCRIBE_ENGINE_TABLE, tier='local'))}"
            )
        if provider == "ollama" and local_endpoint.edge_auth:
            raise LocalTranscribeUnsupportedError(
                "local-server transcription isn't supported through an authenticated front door"
            )
        if ctx.http is None:
            raise RuntimeError("no http client available for remote transcription")
        selected_source = router.credential_source_for(provider)
        routed = routed_admission_in_scope(ctx.extras)
        if routed is not None:
            _halt_unless_consented_credential(
                routed.route,
                selected_source,
                f"this {provider} transcription call",
                context=ctx.credential_use_context,
            )
        request_builder = _TRANSCRIPTION_REQUEST_BUILDERS.get(model_id)
        data = {
            "model": model,
            "response_format": "verbose_json" if "whisper" in model else "json",
        }
        language = _spec_language(spec)
        if language:
            data["language"] = language
        fname = media_upload_filename(path, media)
        content_type = (
            str(media["mime"])
            if isinstance(media, dict) and media.get("mime")
            else "application/octet-stream"
        )
        try:
            if request_builder is not None:
                payload = request_builder(
                    data,
                    path,
                    fname,
                    content_type,
                    spec,
                    transcribe_engine_capabilities(model_id),
                )
                resp = await ctx.http.post(
                    f"{base.rstrip('/')}/audio/transcriptions",
                    json=payload,
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=600.0,
                )
            else:
                resp = await ctx.http.post(
                    f"{base.rstrip('/')}/audio/transcriptions",
                    data=data,
                    files={"file": (fname, Path(path).read_bytes(), content_type)},
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=600.0,
                )
        except (httpx.HTTPError, OSError) as error:
            raise RuntimeError(
                _safe_remote_exception(
                    "remote transcription transport error: ", error, key
                )
            ) from None
        if resp.status_code != 200:
            raise _RemoteTranscriptionResponseError(
                code="http",
                message=redact_text(
                    f"remote transcription failed ({resp.status_code}): {resp.text}",
                    secret_values=(key,),
                    max_chars=300,
                ),
                retryable=resp.status_code == 429 or resp.status_code >= 500,
                accounting=_remote_response_accounting(
                    model_id=model_id,
                    provider=provider,
                    credential_source=selected_source,
                    ctx=ctx,
                    response=resp,
                    warning=f"{provider} returned HTTP {resp.status_code}; request cost is unknown",
                ),
            ) from None
        try:
            out = resp.json()
            if not isinstance(out, dict):
                raise TypeError("response is not an object")
            raw_segments = out.get("segments") or []
            if not isinstance(raw_segments, list):
                raise TypeError("segments is not a list")
            segments = [_segment_with_speaker(segment) for segment in raw_segments]
            usage = out.get("usage") or {}
            if not isinstance(usage, dict):
                raise TypeError("usage is not an object")
            price = audio_price(model_id)
            cost: float | None = None
            if price and "per_second" in price:
                seconds = out.get("duration") or usage.get("seconds")
                if seconds is None and segments:
                    seconds = segments[-1]["end"]
                if price["per_second"] == 0:
                    cost = 0.0
                elif seconds is not None:
                    cost = round(float(seconds) * price["per_second"], 6)
            elif price and usage.get("type") == "tokens":
                cost = round(
                    usage.get("input_tokens", 0) * price["input_per_token"]
                    + usage.get("output_tokens", 0) * price["output_per_token"],
                    6,
                )
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            OverflowError,
        ) as error:
            raise _RemoteTranscriptionResponseError(
                code="invalid_response",
                message=_safe_remote_exception(
                    "malformed remote transcription response: ", error, key
                ),
                accounting=_remote_response_accounting(
                    model_id=model_id,
                    provider=provider,
                    credential_source=selected_source,
                    ctx=ctx,
                    response=resp,
                    warning=f"{provider} returned an unusable transcription response; request cost is unknown",
                ),
            ) from None
        return {
            "text": str(out.get("text", "")).strip(),
            "segments": segments_with_index(segments),
            "language": out.get("language"),
            "cost": cost,
            "usage": usage,
            "duration": out.get("duration") or usage.get("seconds"),
            "credential_source": selected_source,
            "cost_source": "free_local"
            if cost == 0 and provider == "ollama"
            else "pricing_data"
            if cost is not None
            else "unknown",
        }

    def model_calls(
        self, engine: str, path: str, spec: dict[str, Any], out: dict[str, Any]
    ) -> list[dict[str, Any]]:
        return [provider_model_call(engine, path, out).as_dict()]
