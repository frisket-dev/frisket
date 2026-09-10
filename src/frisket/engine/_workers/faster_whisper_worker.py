"""Private local faster-whisper transcription worker.

One-shot, per-row: launched as ``python -m frisket.engine._workers.faster_whisper_worker``
inside the sandboxed subprocess spawned by ``FasterWhisperAdapter`` in
``ops/transcribe_engines.py`` via ``run_sandboxed`` (sandbox/shim.py). The strict local
materialization of ``frisket.transcription.v1`` arrives on stdin and the same
strict success/error envelope used by the gateway is printed to stdout.
``faster_whisper`` is imported only inside ``main`` once the sandbox owns the
process.
"""

from __future__ import annotations

import sys
import time

from pydantic import ValidationError

from frisket.contracts.actions.schemas._engines import (
    project_transcription_engine_options,
)
from frisket.contracts.transcription_sidecar import (
    LocalFasterWhisperRequest,
    TRANSCRIPTION_CONTRACT_VERSION,
    TranscriptionError,
    TranscriptionErrorEnvelope,
    TranscriptionResponse,
    TranscriptionResult,
)


def _emit_error(code: str, message: str) -> None:
    envelope = TranscriptionErrorEnvelope(
        contract_version=TRANSCRIPTION_CONTRACT_VERSION,
        error=TranscriptionError(
            code=code,
            message=message,
            retryable=False,
        ),
    )
    print(envelope.model_dump_json())


def _resolve_pinned_base_snapshot() -> str | None:
    """Resolve the pinned faster-whisper 'base' snapshot from the local
    Hugging Face Hub cache, entirely offline (this worker's sandbox has no
    network -- see ops/transcribe_engines.py's SandboxPolicy; it relies on a
    pre-warmed cache, same as Parakeet). Returns the absolute local snapshot
    directory (which ``WhisperModel`` loads directly, exactly like a Hub repo
    id) when the exact pinned revision is already cached, or ``None`` when
    it isn't -- the caller then falls back to today's unpinned
    ``WhisperModel("base", ...)`` resolution unchanged."""
    try:
        from huggingface_hub import snapshot_download

        from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache
        from frisket.ai.models import artifact_manifest

        entry = artifact_manifest.whisper_base_artifact()
        if entry is None or entry.hf_snapshot is None:
            return None
        snap = entry.hf_snapshot
        return snapshot_download(
            snap.repo_id,
            revision=snap.revision,
            allow_patterns=list(snap.files),
            cache_dir=huggingface_hub_cache(),
            local_files_only=True,
            token=False,
        )
    except Exception:
        return None


def main() -> None:
    try:
        request = LocalFasterWhisperRequest.model_validate_json(
            sys.stdin.read(), strict=True
        )
    except (ValidationError, ValueError):
        _emit_error(
            "invalid_request",
            "request does not conform to the local transcription v1 contract",
        )
        return
    path = request.audio_path
    options = request.options
    supplied_options = options.model_dump(mode="python", exclude_none=True)
    if (
        project_transcription_engine_options("faster_whisper", supplied_options)
        != supplied_options
    ):
        _emit_error(
            "unsupported_option",
            "transcription options are not supported by Faster Whisper",
        )
        return
    size = options.model_size or "base"
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        _emit_error(
            "engine_unavailable",
            "faster-whisper not installed in this environment "
            "(pip install faster-whisper)",
        )
        return
    # Guarded the same way Parakeet guards onnx_asr.load_model: a missing or
    # undownloadable model emits a closed v1 error rather than a traceback.
    model_source = size
    model_ids = [f"faster-whisper/{size}"]
    revision = "runtime-resolved"
    warnings = ["model revision was resolved by the local faster-whisper runtime"]
    if size == "base":
        pinned_path = _resolve_pinned_base_snapshot()
        if pinned_path is not None:
            model_source = pinned_path
            from frisket.ai.models import artifact_manifest

            entry = artifact_manifest.whisper_base_artifact()
            if entry is not None and entry.hf_snapshot is not None:
                model_ids = [entry.hf_snapshot.repo_id]
                revision = entry.hf_snapshot.revision
                warnings = []
    try:
        model = WhisperModel(model_source, device="cpu", compute_type="int8")
    except Exception as error:
        _emit_error(
            "engine_unavailable",
            f"faster-whisper model '{size}' unavailable "
            f"(load failed: {error}) -- pre-populate HF_HOME/HF_HUB_CACHE "
            "before the no-network worker sandbox starts, or use another "
            "transcription engine",
        )
        return
    # VAD defaults on, and word timestamps preserve otherwise-unrecoverable
    # sub-segment evidence.
    started = time.perf_counter()
    try:
        segments, info = model.transcribe(
            path,
            language=options.language,
            vad_filter=True if options.vad is None else options.vad,
            initial_prompt=options.context,
            word_timestamps=True,
        )
    except Exception as error:
        _emit_error(
            "worker_failure",
            f"faster-whisper transcription failed: {error}",
        )
        return
    try:
        segs = []
        for segment in segments:
            entry = {
                "start": round(segment.start, 2),
                "end": round(segment.end, 2),
                "text": segment.text.strip(),
            }
            if segment.words:
                entry["words"] = [
                    {
                        "word": word.word.strip(),
                        "start": round(word.start, 2),
                        "end": round(word.end, 2),
                    }
                    for word in segment.words
                    if str(word.word).strip()
                ]
            segs.append(entry)
        detected_language = str(getattr(info, "language", "") or "").strip()
        reported_duration = getattr(info, "duration", None)
        duration = (
            float(reported_duration)
            if reported_duration is not None
            else max((segment["end"] for segment in segs), default=0.0)
        )
        result = TranscriptionResult(
            engine=request.engine,
            text=" ".join(segment["text"] for segment in segs).strip(),
            segments=segs,
            language=detected_language or options.language,
            duration=duration,
            model_ids=model_ids,
            revision=revision,
            device="cpu",
            dtype="int8",
            timings={"inference_seconds": time.perf_counter() - started},
            warnings=warnings,
            accepted_options=supplied_options,
        )
        response = TranscriptionResponse(
            contract_version=TRANSCRIPTION_CONTRACT_VERSION,
            results=[result],
        )
    except Exception:
        _emit_error(
            "worker_failure",
            "faster-whisper returned an invalid transcription result",
        )
        return
    print(response.model_dump_json(exclude_none=True))


if __name__ == "__main__":
    main()
