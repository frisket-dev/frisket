"""MOSS-Transcribe-Diarize adapter (Boundary C) over its native server (D).

MOSS is served by sgl-omni / vLLM behind an OpenAI-compatible
``/v1/audio/transcriptions`` endpoint.  This adapter is a thin HTTP client to
that server: it spools nothing (the worker wrapper already handed us a local
path), forwards the audio, parses the canonical transcript grammar, and returns
a contract-conformant result.  It imports no torch/vLLM — the heavy runtime is
the separate serving container named in this leaf's Dockerfile — so the whole
adapter is exercisable on CPU with a mocked transport.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
    TranscribeOptions,
    TranscribeResult,
    TranscribeSegment,
    TranscriptionInputError,
)
from frisket_models.transcription.moss import MOSS_DESCRIPTOR as DESCRIPTOR

from .constants import (
    DEFAULT_MAX_COMPLETION_TOKENS,
    ENGINE,
    MODEL_ID,
    MODEL_REVISION,
    SERVED_DEVICE,
    SERVED_DTYPE,
    TRANSCRIPTION_ROUTE,
    build_prompt,
)
from .transcript import parse_moss_transcript

_MAX_FILE_PREFIX = "Maximum file size exceeded"
_MAX_DURATION_PREFIX = "Audio exceeds maximum allowed duration of "
_MAX_CONTEXT_PREFIX = "This model's maximum context length is "
_DURATION_ENV_MARKER = "VLLM_MAX_AUDIO_DECODE_DURATION_S"
_INVALID_AUDIO_MESSAGES = {
    "Invalid or unsupported audio file.",
    "Audio input is too short to produce any tokens.",
}


@dataclass(frozen=True)
class MossConfig:
    """Runtime knobs for the co-located MOSS server.

    The server is bound to loopback inside this leaf's own image, so ``base_url``
    is fixed to localhost and is deliberately NOT env-overridable: pointing the
    adapter at an arbitrary external host would let a same-model-ID server at a
    different revision return a receipt claiming this image's pinned revision.
    device/dtype are absent for the same reason — image-committed constants
    (``SERVED_DEVICE`` / ``SERVED_DTYPE``), not forgeable per-deploy values.
    (Tests inject a transport via ``MossAdapter(config, client=...)``.)
    """

    base_url: str = field(default="http://127.0.0.1:8000", init=False)
    timeout_seconds: float = 600.0
    probe_timeout_seconds: float = 2.0
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS

    def __post_init__(self) -> None:
        for name in ("timeout_seconds", "probe_timeout_seconds"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        if (
            isinstance(self.max_completion_tokens, bool)
            or not isinstance(self.max_completion_tokens, int)
            or self.max_completion_tokens < 1
        ):
            raise ValueError("max_completion_tokens must be a positive integer")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> MossConfig:
        env = os.environ if environ is None else environ
        return cls(
            timeout_seconds=float(
                env.get("FRISKET_MOSS_TIMEOUT_SECONDS", cls.timeout_seconds)
            ),
            probe_timeout_seconds=float(
                env.get("FRISKET_MOSS_PROBE_TIMEOUT_SECONDS", cls.probe_timeout_seconds)
            ),
            max_completion_tokens=int(
                env.get("FRISKET_MOSS_MAX_COMPLETION_TOKENS", cls.max_completion_tokens)
            ),
        )


class MossAdapter:
    """Transcribe one spooled audio file through the MOSS native server."""

    def __init__(self, config: MossConfig, client: httpx.Client | None = None) -> None:
        self._config = config
        # Constructing the client opens no connection; weights residency is the
        # probe's job, not the factory's. Ignore proxy environment variables:
        # this provenance-pinned client must reach loopback directly.
        self._client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            trust_env=False,
        )

    def transcribe(
        self, audio_path: Path, options: TranscribeOptions
    ) -> TranscribeResult:
        accepted_options = options.supplied_options()
        data: dict[str, str] = {
            "model": MODEL_ID,
            "response_format": "json",
            "temperature": "0",
            # vLLM honours ``max_completion_tokens``; ``max_new_tokens`` would be
            # ignored, leaving the model on its ~5k default and truncating long
            # audio.  ``language`` is never sent: MOSS does not consume it, so the
            # descriptor rejects it upstream rather than accept-and-ignore it.
            "max_completion_tokens": str(self._config.max_completion_tokens),
        }
        prompt = build_prompt(options.context)
        if prompt is not None:
            data["prompt"] = prompt

        started = time.monotonic()
        with audio_path.open("rb") as handle:
            files = {"file": (audio_path.name, handle, "application/octet-stream")}
            # Transport exceptions propagate to the shared worker, which owns
            # their retry/status mapping rather than this model-specific leaf.
            response = self._client.post(TRANSCRIPTION_ROUTE, data=data, files=files)
        inference_seconds = time.monotonic() - started

        self._raise_for_status(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(f"MOSS server returned non-JSON body: {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise RuntimeError("MOSS server response is missing a 'text' field")

        parsed_segments = parse_moss_transcript(payload["text"])
        segments = [
            TranscribeSegment(
                start=segment.start,
                end=segment.end,
                text=segment.text,
                # Intrinsic diarization: every segment carries a speaker; MOSS
                # emits no per-word timing, so ``words`` stays absent.
                speaker=segment.speaker,
            )
            for segment in parsed_segments
        ]
        total_seconds = time.monotonic() - started

        return TranscribeResult(
            engine=ENGINE,
            text=" ".join(seg.text for seg in parsed_segments if seg.text),
            segments=segments,
            # MOSS auto-detects and the json response does not report the
            # detected language, so we assert nothing rather than echo a hint.
            language=None,
            # max end across all segments — with overlap the last-by-start
            # segment can close earlier than an earlier, longer one.
            duration=max((segment.end for segment in segments), default=None),
            model_ids=[MODEL_ID],
            revision=MODEL_REVISION,
            device=SERVED_DEVICE,
            dtype=SERVED_DTYPE,
            timings={
                "inference_seconds": inference_seconds,
                "total_seconds": total_seconds,
            },
            # Parser anomalies fail closed, so a successful result is complete
            # and needs no parser-degradation warning.
            warnings=[],
            accepted_options=accepted_options,
        )

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        status = response.status_code
        error = _vllm_error(response)
        detail = error.message

        if status == 413:
            raise TranscriptionInputError(
                f"audio exceeds the MOSS server input bound: {detail}",
                too_large=True,
            )

        if status == 400:
            # These are the pinned vLLM server's structured forms for its file,
            # decoded-duration, and rendered-input context bounds.  The native
            # endpoint reports them as 400; contract v1 requires 413.
            if error.param == "audio_filesize_mb" and detail.startswith(
                _MAX_FILE_PREFIX
            ):
                raise TranscriptionInputError(
                    f"audio exceeds the MOSS server input bound: {detail}",
                    too_large=True,
                )
            if (
                detail.startswith(_MAX_DURATION_PREFIX)
                and _DURATION_ENV_MARKER in detail
            ):
                raise TranscriptionInputError(
                    f"audio exceeds the MOSS server input bound: {detail}",
                    too_large=True,
                )
            if error.param in {"input_text", "input_tokens"} and detail.startswith(
                _MAX_CONTEXT_PREFIX
            ):
                raise TranscriptionInputError(
                    f"input exceeds the MOSS model context bound: {detail}",
                    too_large=True,
                )
            if error.param is None and detail in _INVALID_AUDIO_MESSAGES:
                raise TranscriptionInputError(f"MOSS rejected the audio: {detail}")

            # Any other pinned-server 400 is our request/configuration bug or an
            # upstream protocol change.  Do not blame the caller's audio based
            # on a broad substring match.
            raise RuntimeError(
                "MOSS returned an unclassified HTTP 400 "
                f"(type={error.error_type!r}, param={error.param!r}): {detail}"
            )

        if status == 415:
            raise TranscriptionInputError(f"MOSS rejected the audio: {detail}")

        # 422 (request-schema validation = our bug), 5xx, and every unknown
        # status are server faults, mapped to a 500 by the worker.
        raise RuntimeError(f"MOSS server returned HTTP {status}: {detail}")


@dataclass(frozen=True, slots=True)
class _VllmError:
    message: str
    error_type: str | None
    param: str | None


def _vllm_error(response: httpx.Response) -> _VllmError:
    try:
        body = response.json()
    except ValueError:
        return _VllmError(
            message=response.text[:200] or f"HTTP {response.status_code}",
            error_type=None,
            param=None,
        )
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        error = body["error"]
        message = error.get("message")
        error_type = error.get("type")
        param = error.get("param")
        return _VllmError(
            message=message
            if isinstance(message, str) and message
            else str(body)[:200],
            error_type=error_type if isinstance(error_type, str) else None,
            param=param if isinstance(param, str) else None,
        )
    return _VllmError(message=str(body)[:200], error_type=None, param=None)


def build_probe(config: MossConfig | None = None):
    """Return a cheap liveness probe for the MOSS server.

    The probe never cold-loads: it issues one short-timeout ``GET /health`` and
    reports the model-specific truth for ``loaded``.  A served MOSS process only
    answers ``/health`` 200 once its weights are resident, so a healthy server
    is reported ``available`` and ``loaded``.
    """

    resolved = config or MossConfig.from_env()

    def probe() -> EngineProbe:
        try:
            response = httpx.get(
                f"{resolved.base_url}/health",
                timeout=resolved.probe_timeout_seconds,
                trust_env=False,
            )
        except httpx.HTTPError as exc:
            return EngineProbe(
                available=False,
                loaded=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        if response.is_success:
            return EngineProbe(available=True, loaded=True, error=None)
        return EngineProbe(
            available=False,
            loaded=False,
            error=f"MOSS server health returned HTTP {response.status_code}",
        )

    return probe


def build_registration(config: MossConfig | None = None) -> AdapterRegistration:
    """Assemble the lazy worker registration for MOSS."""

    resolved = config or MossConfig.from_env()
    return AdapterRegistration(
        descriptor=DESCRIPTOR,
        factory=lambda: MossAdapter(resolved),
        probe=build_probe(resolved),
    )


REGISTRATION = build_registration()
