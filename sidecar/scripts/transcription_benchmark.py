#!/usr/bin/env python3
"""Redacted gateway benchmark that omits transcript, audio path, context text, warnings, and bearer tokens."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier
from typing import Any, TextIO
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from frisket_models.transcription.contract import (
    CONTRACT_VERSION,
    GatewayTranscriptionResponse,
    TranscribeOptions,
    TranscribeResult,
    TranscriptionEngineDescriptor,
    TranscriptionErrorEnvelope,
)

BENCHMARK_SCHEMA = "frisket.transcription.benchmark.v1"
PROBE_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
INFERENCE_TIMEOUT = httpx.Timeout(
    3600.0,
    connect=10.0,
    write=60.0,
    pool=10.0,
)


class BenchmarkError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    base_url: str
    engine: str
    audio_path: Path
    options: TranscribeOptions
    warmups: int = 1
    concurrencies: tuple[int, ...] = (1, 2)
    waves: int = 2
    require_cold: bool = False
    audio_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        base_url = self.base_url.strip().rstrip("/")
        parsed = urlsplit(base_url)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise BenchmarkError("--base-url must be an HTTP(S) origin")
        object.__setattr__(self, "base_url", base_url)
        if not (engine := self.engine.strip()):
            raise BenchmarkError("--engine must not be empty")
        object.__setattr__(self, "engine", engine)
        audio = self.audio_path.resolve()
        if not audio.is_file():
            raise BenchmarkError("--audio must name a readable regular file")
        object.__setattr__(self, "audio_path", audio)
        digest = hashlib.sha256()
        with audio.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        object.__setattr__(self, "audio_sha256", digest.hexdigest())
        if self.warmups < 0 or self.waves < 1:
            raise BenchmarkError("--warmups must be nonnegative and --waves at least 1")
        if not self.concurrencies:
            raise BenchmarkError("--concurrency must contain a value")
        if any(value < 1 or value > 256 for value in self.concurrencies):
            raise BenchmarkError("--concurrency values must be between 1 and 256")


def _safe_option_receipt(options: TranscribeOptions) -> dict[str, Any]:
    receipt = options.model_dump(mode="json", exclude_none=True)
    context = receipt.pop("context", None)
    if context is not None:
        receipt["context"] = {
            "present": True,
            "utf8_bytes": len(context.encode("utf-8")),
        }
    return receipt


def _capability_record(
    client: httpx.Client,
    config: BenchmarkConfig,
    *,
    token: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        response = client.get(
            f"{config.base_url}/capabilities",
            headers={"Authorization": f"Bearer {token}"},
            timeout=PROBE_TIMEOUT,
        )
    except httpx.RequestError as exc:
        raise BenchmarkError("capability probe could not reach gateway") from exc
    if response.status_code != 200:
        raise BenchmarkError(f"capability probe returned HTTP {response.status_code}")
    try:
        payload = response.json()
        engine = next(
            item for item in payload["engines"] if item.get("name") == config.engine
        )
        descriptor = TranscriptionEngineDescriptor.model_validate(
            {
                "engine": config.engine,
                "model_ids": engine["models"],
                "revision": engine["revision"],
                "runtime_image_id": engine["runtime_image_id"],
                "options": engine["options"],
            }
        )
        if CONTRACT_VERSION not in engine["contract_versions"]:
            raise ValueError("engine does not advertise contract v1")
    except (KeyError, StopIteration, TypeError, ValidationError, ValueError) as exc:
        raise BenchmarkError(
            "gateway capability response violates contract v1"
        ) from exc
    return {
        "type": "capability",
        "engine": descriptor.engine,
        "available": engine["available"] and engine.get("error") is None,
        "loaded": engine["loaded"],
        "latency_seconds": round(time.perf_counter() - started, 6),
        "model_ids": list(descriptor.model_ids),
        "revision": descriptor.revision,
        "runtime_image_id": descriptor.runtime_image_id,
        "options": descriptor.options.model_dump(mode="json"),
    }


def _result_metrics(result: TranscribeResult) -> dict[str, Any]:
    return {
        "duration_seconds": result.duration,
        "segment_count": len(result.segments),
        "word_count": sum(len(segment.words or ()) for segment in result.segments),
        "speaker_count": len(
            {
                segment.speaker
                for segment in result.segments
                if segment.speaker is not None
            }
        ),
        "warning_count": len(result.warnings),
        "model_ids": list(result.model_ids),
        "revision": result.revision,
        "device": result.device,
        "dtype": result.dtype,
        "timing_seconds": dict(sorted(result.timings.items())),
    }


def _request_record(
    client: httpx.Client,
    config: BenchmarkConfig,
    *,
    token: str,
    phase: str,
    concurrency: int,
    wave: int | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "type": "request",
        "phase": phase,
        "concurrency": concurrency,
        "wave": wave,
    }
    data = {
        "contract_version": CONTRACT_VERSION,
        "engine": config.engine,
        "options": json.dumps(
            config.options.model_dump(mode="json", exclude_none=True),
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
    suffix = config.audio_path.suffix.lower() or ".audio"
    started = time.perf_counter()
    try:
        with config.audio_path.open("rb") as audio:
            response = client.post(
                f"{config.base_url}/v1/transcribe",
                data=data,
                files={
                    "files": (
                        f"benchmark{suffix}",
                        audio,
                        "application/octet-stream",
                    )
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=INFERENCE_TIMEOUT,
            )
    except httpx.RequestError:
        record.update(
            status_code=None,
            success=False,
            error_code="transport_error",
            latency_seconds=round(time.perf_counter() - started, 6),
            rtf=None,
        )
        return record

    latency = round(time.perf_counter() - started, 6)
    record.update(status_code=response.status_code, latency_seconds=latency, rtf=None)
    if response.status_code != 200:
        try:
            error = TranscriptionErrorEnvelope.model_validate_json(
                response.content,
                strict=True,
            ).error
            error_code = error.code
        except (ValidationError, ValueError):
            error_code = f"http_{response.status_code}"
        record.update(success=False, error_code=error_code)
        return record

    try:
        envelope = GatewayTranscriptionResponse.model_validate_json(
            response.content,
            strict=True,
        )
        if len(envelope.results) != 1 or envelope.results[0].engine != config.engine:
            raise ValueError("one result from the requested engine is required")
        result = envelope.results[0]
    except (ValidationError, ValueError):
        record.update(success=False, error_code="invalid_success_envelope")
        return record
    duration = result.duration
    if duration is not None and duration > 0:
        record["rtf"] = round(latency / duration, 6)
    record.update(success=True, error_code=None, result=_result_metrics(result))
    return record


def _run_wave(
    client: httpx.Client,
    config: BenchmarkConfig,
    *,
    token: str,
    concurrency: int,
    wave: int,
) -> tuple[list[dict[str, Any]], float]:
    barrier = Barrier(concurrency)

    def one(_slot: int) -> dict[str, Any]:
        barrier.wait()
        return _request_record(
            client,
            config,
            token=token,
            phase="measure",
            concurrency=concurrency,
            wave=wave,
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        observations = list(executor.map(one, range(1, concurrency + 1)))
    return observations, time.perf_counter() - started


def _summary(
    observations: Sequence[dict[str, Any]],
    *,
    concurrency: int,
    waves: int,
    wall_seconds: float,
) -> dict[str, Any]:
    successes = [item for item in observations if item["success"]]
    processed = sum(item["result"]["duration_seconds"] or 0.0 for item in successes)
    return {
        "type": "concurrency_summary",
        "concurrency": concurrency,
        "waves": waves,
        "request_count": len(observations),
        "success_count": len(successes),
        "at_capacity_count": sum(
            item["error_code"] == "at_capacity" for item in observations
        ),
        "wall_seconds": round(wall_seconds, 6),
        "throughput_requests_per_second": round(len(observations) / wall_seconds, 6),
        "processed_audio_seconds_per_wall_second": round(processed / wall_seconds, 6),
    }


def run_benchmark(
    config: BenchmarkConfig,
    *,
    token: str,
    output: TextIO,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    if not token or any(character.isspace() for character in token):
        raise BenchmarkError("bearer token must be non-empty and contain no whitespace")
    owned_client = client is None
    if client is None:
        connections = max(config.concurrencies)
        client = httpx.Client(
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=connections,
                max_keepalive_connections=connections,
            ),
        )
    records: list[dict[str, Any]] = []

    def emit(record: dict[str, Any]) -> None:
        complete = {"schema": BENCHMARK_SCHEMA, **record}
        records.append(complete)
        print(json.dumps(complete, sort_keys=True, separators=(",", ":")), file=output)
        output.flush()

    def serial_request(phase: str) -> None:
        request = _request_record(
            client,
            config,
            token=token,
            phase=phase,
            concurrency=1,
            wave=None,
        )
        emit(request)
        if not request["success"]:
            raise BenchmarkError(
                f"{phase} request failed with "
                f"{request['error_code'] or 'unknown_error'}"
            )

    try:
        emit(
            {
                "type": "run_start",
                "engine": config.engine,
                "audio_sha256": config.audio_sha256,
                "option_receipt": _safe_option_receipt(config.options),
                "warmups": config.warmups,
                "concurrencies": list(config.concurrencies),
                "waves": config.waves,
                "require_cold": config.require_cold,
            }
        )
        capability = _capability_record(client, config, token=token)
        emit(capability)
        if not capability["available"]:
            raise BenchmarkError(f"engine {config.engine!r} is unavailable")
        if config.require_cold and capability["loaded"]:
            raise BenchmarkError(
                f"engine {config.engine!r} is already loaded; cold run required"
            )

        baseline_phase = "baseline" if capability["loaded"] else "cold"
        serial_request(baseline_phase)
        for _ in range(config.warmups):
            serial_request("warmup")

        for concurrency in config.concurrencies:
            measured: list[dict[str, Any]] = []
            measured_wall_seconds = 0.0
            for wave in range(1, config.waves + 1):
                observations, wall_seconds = _run_wave(
                    client,
                    config,
                    token=token,
                    concurrency=concurrency,
                    wave=wave,
                )
                measured.extend(observations)
                measured_wall_seconds += wall_seconds
                for observation in observations:
                    emit(observation)
            emit(
                _summary(
                    measured,
                    concurrency=concurrency,
                    waves=config.waves,
                    wall_seconds=measured_wall_seconds,
                )
            )
        emit({"type": "run_end", "status": "succeeded"})
    except Exception:
        emit({"type": "run_end", "status": "failed"})
        raise
    finally:
        if owned_client:
            client.close()
    return records


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--audio", type=Path, required=True)
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--options-json", default="{}")
    options.add_argument("--options-file", type=Path)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--concurrency", default="1,2")
    parser.add_argument("--waves", type=int, default=2)
    parser.add_argument("--require-cold", action="store_true")
    parser.add_argument("--token-env", default="FRISKET_MODELS_TOKEN")
    parser.add_argument(
        "--output",
        default="-",
        help="JSONL destination; '-' writes stdout (files are never overwritten)",
    )
    args = parser.parse_args(argv)
    try:
        raw_options = (
            args.options_file.read_text(encoding="utf-8")
            if args.options_file is not None
            else args.options_json
        )
        token = os.environ.get(args.token_env)
        if token is None:
            raise BenchmarkError(
                f"required bearer token environment variable {args.token_env!r} is unset"
            )
        try:
            option_values = json.loads(raw_options)
            parsed_options = TranscribeOptions.model_validate(
                option_values,
                strict=True,
            )
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise BenchmarkError("transcription options are invalid") from exc
        config = BenchmarkConfig(
            base_url=args.base_url,
            engine=args.engine,
            audio_path=args.audio,
            options=parsed_options,
            warmups=args.warmups,
            concurrencies=tuple(
                int(item) for item in args.concurrency.split(",") if item.strip()
            ),
            waves=args.waves,
            require_cold=args.require_cold,
        )
        if args.output == "-":
            run_benchmark(config, token=token, output=sys.stdout)
        else:
            with Path(args.output).open("x", encoding="utf-8") as output:
                run_benchmark(config, token=token, output=output)
    except (BenchmarkError, OSError, ValidationError, ValueError) as exc:
        print(f"benchmark failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
