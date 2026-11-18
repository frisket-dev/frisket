"""Hosted OpenAI-dialect transcription adapters, including MAI."""

from __future__ import annotations

import base64
import math
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
from frisket.execution.dispatch_control import dispatch_for_context
from frisket.execution.purchases import (
    PurchaseObservation,
    PurchaseRequest,
    execute_purchase,
    policy_for_context,
)
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseContext,
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.local_model_ids import bare_model_name
from frisket.ops.base import ActionContext, RecipeInvocationHalt, media_upload_filename
from frisket.ops.integrations.hosted_error import (
    HostedEngineError,
    transport_outcome_is_uncertain,
)
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


def _estimate_transcription_cost(
    model_id: str, duration_seconds: float | None
) -> float | None:
    """Rough purchase headroom; the response's measured usage owns settlement."""
    price = audio_price(model_id)
    if price is None or duration_seconds is None:
        return None
    if "per_second" in price:
        return duration_seconds * price["per_second"]
    if "input_per_token" in price and "output_per_token" in price:
        # Deliberately approximate audio encoding and transcript density. These
        # are admission estimates, never billable usage or a whole-run ceiling.
        audio_tokens = math.ceil(duration_seconds * 25)
        transcript_tokens = math.ceil(duration_seconds * 5)
        return (
            audio_tokens * price["input_per_token"]
            + transcript_tokens * price["output_per_token"]
        )
    return None


def _remote_response_accounting(
    *,
    model_id: str,
    provider: str,
    credential_source: str,
    ctx: ActionContext,
    response: httpx.Response,
    warning: str,
    known_usage: dict[str, Any] | None = None,
    known_cost: float | None = None,
) -> dict[str, Any]:
    price = audio_price(model_id)
    known_zero = price is not None and all(
        isinstance(rate, (int, float)) and not isinstance(rate, bool) and rate == 0
        for rate in price.values()
    )
    provider_cost = (
        known_cost if known_cost is not None else 0.0 if known_zero else None
    )
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
            if provider_cost is not None
            else "unknown"
        ),
        units={"requests": 1, **(known_usage or {})},
        request_id=response.headers.get("x-request-id")
        or response.headers.get("request-id"),
        warnings=[] if provider_cost is not None else [warning],
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
        self, model_id: str, path: str, spec: dict, ctx: ActionContext, media: Any
    ) -> dict:
        spec = project_transcription_engine_options(model_id, spec)
        provider, _, model = model_id.partition("/")
        router = (ctx.extras or {}).get("router")
        from frisket.ai.llm.router import _transcription_connection

        base, key, edge_auth = None, None, False
        if router is not None:
            try:
                base, key, model, edge_auth = _transcription_connection(
                    router, model_id
                )
            except LLMError as exc:
                raise RuntimeError(str(exc)) from exc
        if not base or not key:
            raise RuntimeError(
                f"remote transcription needs a configured '{provider}' "
                f"provider (set {provider.upper()}_API_KEY or an org key) — "
                f"or use a local engine: {', '.join(engine_ids(TRANSCRIBE_ENGINE_TABLE, tier='local'))}"
            )
        if provider == "ollama" and edge_auth:
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
        file_bytes = Path(path).read_bytes()
        payload = (
            request_builder(
                data,
                path,
                fname,
                content_type,
                spec,
                transcribe_engine_capabilities(model_id),
            )
            if request_builder is not None
            else None
        )
        from hashlib import sha256
        from frisket.ops.media_probe import probe_for_ingest

        digest = sha256(file_bytes).hexdigest()
        duration = probe_for_ingest(Path(path), digest=digest).get("duration_seconds")
        request = PurchaseRequest(
            provider=provider,
            capability="transcribe",
            engine=model_id,
            prepared={
                "audio_digest": digest,
                "mime": content_type,
                "request": data,
                "options": spec,
                "audio_seconds": duration,
            },
            credential_source=selected_source,
            estimated_cost_usd=_estimate_transcription_cost(model_id, duration),
            context=ctx,
        )

        async def invoke():
            try:
                response = await ctx.http.post(
                    f"{base.rstrip('/')}/audio/transcriptions",
                    **(
                        {"json": payload}
                        if payload is not None
                        else {
                            "data": data,
                            "files": {"file": (fname, file_bytes, content_type)},
                        }
                    ),
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=600.0,
                )
            except (httpx.HTTPError, OSError) as error:
                raise _RemoteTranscriptionResponseError(
                    code="transport",
                    message=_safe_remote_exception(
                        "remote transcription transport error: ", error, key
                    ),
                    retryable=True,
                    post_egress_ambiguous=transport_outcome_is_uncertain(error),
                    purchase_dispatched=(
                        None if transport_outcome_is_uncertain(error) else False
                    ),
                ) from None
            if response.status_code != 200:
                error = _RemoteTranscriptionResponseError(
                    code="http",
                    message=redact_text(
                        f"remote transcription failed ({response.status_code}): {response.text}",
                        secret_values=(key,),
                        max_chars=300,
                    ),
                    retryable=response.status_code == 429
                    or response.status_code >= 500,
                    accounting=_remote_response_accounting(
                        model_id=model_id,
                        provider=provider,
                        credential_source=selected_source,
                        ctx=ctx,
                        response=response,
                        warning=f"{provider} returned HTTP {response.status_code}; request cost is unknown",
                    ),
                )
                error.status_code = response.status_code
                raise error from None
            return response

        out: Any = None
        usage: dict[str, Any] = {}
        cost: float | None = None
        parse_error: Exception | None = None
        observed_usage: dict[str, Any] = {}

        def observe(response):
            nonlocal out, usage, cost, parse_error, observed_usage
            try:
                out = response.json()
                if not isinstance(out, dict):
                    raise TypeError("response is not an object")
                raw_segments = out.get("segments") or []
                usage = out.get("usage") or {}
                if not isinstance(usage, dict):
                    raise TypeError("usage is not an object")
                seconds = out.get("duration")
                if seconds is None:
                    seconds = usage.get("seconds")
                if (
                    seconds is None
                    and isinstance(raw_segments, list)
                    and raw_segments
                    and isinstance(raw_segments[-1], dict)
                ):
                    seconds = raw_segments[-1].get("end")
                observed_usage = dict(usage)
                if seconds is not None:
                    observed_usage["audio_seconds"] = seconds
                price = audio_price(model_id)
                if price and "per_second" in price:
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
                if cost is not None and (not math.isfinite(cost) or cost < 0):
                    cost = None
                    raise ValueError(
                        "provider usage must produce a finite nonnegative cost"
                    )
            except (
                ValueError,
                TypeError,
                KeyError,
                IndexError,
                AttributeError,
                OverflowError,
            ) as error:
                parse_error = error
            return PurchaseObservation(
                result=out
                if out is not None
                else {
                    "http_status": response.status_code,
                    "body": redact_text(
                        response.text, secret_values=(key,), max_chars=300
                    ),
                },
                usage=observed_usage,
                provider_cost_micro=None if cost is None else round(cost * 1_000_000),
            )

        resp = await execute_purchase(
            request,
            policy=policy_for_context(ctx),
            dispatch=dispatch_for_context(ctx),
            invoke=invoke,
            observe=observe,
        )
        # Accounting owns the returned response before transcript validation or
        # publication, including when cancellation happened during the request.
        try:
            if parse_error is not None:
                raise parse_error
            raw_segments = out.get("segments") or []
            if not isinstance(raw_segments, list):
                raise TypeError("segments is not a list")
            segments = [_segment_with_speaker(segment) for segment in raw_segments]
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
                    known_usage=observed_usage,
                    known_cost=cost,
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
