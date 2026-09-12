import frisket.sdk.ops.transcribe_engines as transcribe_engines
import asyncio
import io
import json
import math
import os
import shutil
import signal
import struct
import subprocess
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from frisket.contracts.transcription_sidecar import (
    TRANSCRIPTION_CONTRACT_VERSION,
    parse_transcription_response,
)
from frisket.engine._workers.parakeet_artifacts import (
    ParakeetArtifacts,
    huggingface_hub_cache,
)
from frisket.engine._workers.parakeet_session import (
    parakeet_inference_env,
    parakeet_inference_policy,
    parakeet_init_frame,
)
from frisket.ai.llm import ModelRouter
from frisket.ai.llm.pricing import audio_price
from frisket.contracts.actions.schemas import media
from frisket.contracts.actions.schemas._engines import (
    EngineDeclaration,
    TranscriptionEngineCapabilities,
)
from frisket.ops.base import OpContext, Recipe
from frisket.actions.media import TranscribeParams
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest, SheetRows
from frisket.engine.executor.map_rows_action import (
    build_typed_map_rows_plan,
    run_typed_map_rows_action,
)
from frisket.sdk.ops.transcription.faster_whisper import _worker_failure_message
from frisket.engine.sandbox.shim import SandboxResult
from tests.execution_composition_helpers import open_attempt_authority


def _local_worker_request(path: str, **options) -> str:
    return json.dumps(
        {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "engine": "faster-whisper",
            "audio_path": path,
            "options": options,
        }
    )


def _local_worker_response(*, accepted_options: dict | None = None) -> str:
    return json.dumps(
        {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [
                {
                    "engine": "faster-whisper",
                    "text": "",
                    "segments": [],
                    "language": None,
                    "duration": 0.0,
                    "model_ids": ["faster-whisper/base"],
                    "revision": "runtime-resolved",
                    "device": "cpu",
                    "dtype": "int8",
                    "timings": {"inference_seconds": 0.0},
                    "warnings": [],
                    "accepted_options": accepted_options or {},
                }
            ],
        }
    )


def test_vibevoice_sidecar_request_omits_unsupported_clean_default(
    monkeypatch, tmp_path
) -> None:
    """The UI option projection must not send unsupported knobs to v1 workers."""
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    captured: dict[str, object] = {}

    async def fake_sidecar_post(*_args, **kwargs):
        captured.update(kwargs["data"])
        options = json.loads(kwargs["data"]["options"])
        return {
            "contract_version": TRANSCRIPTION_CONTRACT_VERSION,
            "results": [
                {
                    "engine": "vibevoice-asr",
                    "text": "",
                    "segments": [],
                    "language": None,
                    "duration": 0.0,
                    "model_ids": ["vibevoice-asr"],
                    "revision": "runtime-resolved",
                    "device": "cpu",
                    "dtype": "int8",
                    "timings": {"inference_seconds": 0.0},
                    "warnings": [],
                    "accepted_options": options,
                }
            ],
        }

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.sidecar_post", fake_sidecar_post
    )
    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.sidecar.route_gateway_connection",
        lambda *_args, **_kwargs: SimpleNamespace(timeout_seconds=None),
    )

    asyncio.run(
        transcribe_engines.TranscriptionV1Adapter().transcribe(
            "vibevoice-asr",
            str(audio),
            {"clean": False, "context": "names"},
            OpContext(),
        )
    )

    assert json.loads(captured["options"]) == {"context": "names"}


def _wav(path, seconds=0.5, freq=0.0):
    """PCM16 mono 16 kHz wav; freq=0 -> silence."""
    sr = 16000
    n = int(sr * seconds)
    frames = b"".join(
        struct.pack(
            "<h", int(0 if not freq else 12000 * math.sin(2 * math.pi * freq * i / sr))
        )
        for i in range(n)
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return path


def _transcribe_plan(project, sheet, *, output="transcript", **options):
    request = ActionRequest(
        action_id="media.transcribe",
        scope=SheetRows(sheet_id=sheet),
        params={"source": "audio", **options},
        output_names={"text": output, "segments": output + "_segments"},
        idempotency_key="engine-test-" + output,
    )
    return build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request),
    )


def _parakeet_cached() -> bool:
    hub = (
        Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    return any(hub.glob("models--*parakeet-tdt-0.6b-v3*"))


# ---------------------------------------------------------------------------
# contract


def test_engine_param_contract():
    """Typed params own both the engine reference and its scalar options."""
    params = TranscribeParams(source="audio")
    assert params.engine.root == "faster_whisper"
    assert params.vad is True
    assert params.context is None
    assert params.clean is False


@pytest.mark.parametrize(
    ("engine", "transport", "adapter_type"),
    [
        ("faster_whisper", "local", transcribe_engines.FasterWhisperAdapter),
        ("parakeet-tdt", "local", transcribe_engines.ParakeetAdapter),
        (
            "whisper-turbo",
            "frisket.transcription.v1",
            transcribe_engines.TranscriptionV1Adapter,
        ),
        (
            "parakeet-tdt",
            "frisket.transcription.v1",
            transcribe_engines.TranscriptionV1Adapter,
        ),
        ("openai/whisper-1", "remote", transcribe_engines.OpenAITranscriptionAdapter),
    ],
)
def test_shared_engine_boundary_dispatches_each_existing_family(
    monkeypatch, engine, transport, adapter_type
):
    async def fake_transcribe(self, *args, **kwargs):
        return {"text": adapter_type.__name__}

    def fake_model_calls(self, *args, **kwargs):
        return [{"adapter": adapter_type.__name__}]

    monkeypatch.setattr(adapter_type, "transcribe", fake_transcribe)
    monkeypatch.setattr(adapter_type, "model_calls", fake_model_calls)

    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            engine,
            "/nonexistent.wav",
            {"engine": engine},
            OpContext(),
            transport=transport,
        )
    )

    assert result.output == {"text": adapter_type.__name__}
    assert result.model_calls == ({"adapter": adapter_type.__name__},)


def test_unknown_engine_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown transcription engine"):
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "bogus", str(tmp_path / "a.wav"), {}, OpContext(), transport="local"
            )
        )


def test_local_asr_sandbox_env_boundaries(tmp_path, monkeypatch):
    """Both local engines pin the hub cache in the parent; neither inherits it.

    This assertion used to run the other way for faster-whisper -- it required
    the three hub variables to be in ``env_passthrough`` -- which pinned the
    defect rather than catching it. Inheriting them let the child resolve its
    own cache, and the sandbox re-asserts HOME to a per-run scratch dir, so on
    a default install (none of the three set) the child re-downloaded the model
    every transcription while diagnostics reported it ready.
    """
    captured: list[tuple[list[str], dict[str, str]]] = []

    async def fake_run_sandboxed(
        cmd, *, policy, stdin_data, should_cancel=None, extra_env=None
    ):
        captured.append((policy.env_passthrough, dict(extra_env or {})))
        request = json.loads(stdin_data)
        return SandboxResult(
            returncode=0,
            stdout=_local_worker_response(accepted_options=request["options"]),
            stderr="",
        )

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.faster_whisper.run_sandboxed", fake_run_sandboxed
    )

    asyncio.run(
        transcribe_engines.FasterWhisperAdapter().transcribe(
            str(tmp_path / "a.wav"), {}
        )
    )

    hub_vars = {"HF_HOME", "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"}
    assert len(captured) == 1
    whisper_passthrough, whisper_extra = captured[0]
    assert hub_vars.isdisjoint(whisper_passthrough)
    assert whisper_extra == {"HF_HUB_CACHE": str(huggingface_hub_cache())}

    artifacts = ParakeetArtifacts(
        cache_dir=tmp_path / "cache",
        model_path=tmp_path / "model",
        vad_path=tmp_path / "vad",
    )
    policy = parakeet_inference_policy(2)
    assert hub_vars.isdisjoint(policy.env_passthrough)
    assert policy.allow_network is False
    assert policy.trusted_python_netwall is True
    assert parakeet_inference_env(artifacts) == {
        "HF_HUB_CACHE": str(tmp_path / "cache"),
        "HF_HUB_OFFLINE": "1",
    }


def test_parakeet_worker_owns_cpu_provider_and_lean_init_frame(tmp_path, monkeypatch):
    """Local Parakeet must not accidentally select CoreML on macOS.

    Full podcast MP3s can make CoreML compile huge graph partitions, leading to
    OS-killed workers with only ONNX warning noise in stderr. CPU-only is the
    stable local default, owned by the worker as constants rather than
    negotiated over the wire; the provider test executes the worker against
    fake ONNX modules to prove the override wins, while this test pins the
    constants and the lean init frame (schema/artifact pins + VAD flag only).
    """
    from frisket.engine._workers import parakeet_worker

    assert parakeet_worker.ONNX_PROVIDERS == ["CPUExecutionProvider"]
    assert parakeet_worker.ONNX_QUANTIZATION == "int8"
    # All onnx-asr sessions share one bounded pool. A separate adaptive pool
    # for every resampler/model/VAD session exceeded the worker's 4 GiB
    # sandbox; the shared pool reserves one CPU for colocated work and uses the
    # remaining cores without multiplying pools.
    assert parakeet_worker.ONNX_GLOBAL_INTRA_OP_CAP == 4
    assert parakeet_worker.ONNX_INTER_OP_THREADS == 1
    assert parakeet_worker.ONNX_FALLBACK_INTRA_OP_THREADS == 1
    for cores, expected in ((None, 1), (1, 1), (2, 1), (4, 3), (64, 4)):
        monkeypatch.setattr(parakeet_worker.os, "cpu_count", lambda: cores)
        assert parakeet_worker._onnx_global_intra_op_threads() == expected
    frame = parakeet_init_frame(
        ParakeetArtifacts(
            cache_dir=tmp_path / "cache",
            model_path=tmp_path / "model",
            vad_path=tmp_path / "vad",
        ),
        vad=True,
    )
    assert set(frame) == {"schema_version", "type", "artifacts", "vad"}
    assert frame["vad"] is True


def test_transcribe_run_provenance_names_the_engine_not_a_stale_llm_model(tmp_path):
    # A transcribe run's `model` provenance must be the engine (parakeet /
    # faster_whisper), never the leftover default LLM model the spec still
    # carries — transcription is not an LLM call.
    from frisket.engine.store import Project

    project = Project.create(tmp_path / "provenance.frisket")
    sheet = project.add_sheet("Audio")
    project.add_column(sheet, "audio", type="audio")
    plan = _transcribe_plan(project, sheet, engine="parakeet-tdt")
    recipe = plan.program
    assert recipe.run_provenance_model({"engine": "parakeet-tdt"}) == "parakeet-tdt"
    assert (
        recipe.run_provenance_model(
            {"engine": "parakeet-tdt", "model": "gemini/gemini-2.5-flash"}
        )
        == "parakeet-tdt"
    )
    # A non-LLM recipe that keeps the base method reports None (provenance
    # reads "—") rather than a misleading stale model; an LLM recipe still
    # reports its spec model.
    project.add_column(sheet, "url", type="link")
    download_request = ActionRequest(
        action_id="media.ytdlp_download",
        scope=SheetRows(sheet_id=sheet),
        params={"source": "url"},
        idempotency_key="download-provenance",
    )
    download = build_typed_map_rows_plan(
        project,
        BoundTypedActionRequest.bind(
            ACTION_REGISTRY.get("media.ytdlp_download"), download_request
        ),
    ).program
    assert download.llm is False
    assert download.run_provenance_model({"model": "gemini/gemini-2.5-flash"}) is None

    class _LlmRecipe(Recipe):
        consumes_resolution = False
        cost_class = "metered"

    summarize = _LlmRecipe()
    assert summarize.is_llm({}) is True
    assert (
        summarize.run_provenance_model({"model": "gemini/gemini-2.5-flash"})
        == "gemini/gemini-2.5-flash"
    )
    project.close()


@pytest.mark.skipif(
    not hasattr(signal, "SIGKILL"), reason="SIGKILL is a POSIX-only signal"
)
def test_worker_failure_message_reports_signal_without_onnx_warning_noise():
    stderr = (
        "2026-06-23 02:44:17.551 [W:onnxruntime:, "
        "coreml_execution_provider.cc:113 GetCapability] "
        "CoreMLExecutionProvider::GetCapability\n"
        "Context leak detected, msgtracer returned -1\n"
    )
    message = _worker_failure_message(
        SandboxResult(
            returncode=-signal.SIGKILL,
            stdout="",
            stderr=stderr,
        )
    )
    assert "signal 9" in message
    assert "SIGKILL" in message
    assert "CoreMLExecutionProvider" not in message
    assert "msgtracer" not in message


def test_worker_failure_message_keeps_traceback_after_filtering_onnx_noise():
    stderr = (
        "2026-06-23 [W:onnxruntime:, coreml_execution_provider.cc:113 "
        "GetCapability] CoreMLExecutionProvider::GetCapability\n"
        "Traceback (most recent call last):\n"
        '  File "<string>", line 1, in <module>\n'
        "RuntimeError: actual decode failure\n"
    )
    message = _worker_failure_message(
        SandboxResult(
            returncode=1,
            stdout="",
            stderr=stderr,
        )
    )
    assert "actual decode failure" in message
    assert "CoreMLExecutionProvider" not in message


def test_worker_failure_message_handles_missing_or_bytes_stderr():
    none_message = _worker_failure_message(
        SandboxResult(
            returncode=2,
            stdout="",
            stderr=None,
        )
    )
    assert "worker exited with code 2" in none_message

    bytes_message = _worker_failure_message(
        SandboxResult(
            returncode=1,
            stdout="",
            stderr=b"RuntimeError: byte stderr detail",
        )
    )
    assert "byte stderr detail" in bytes_message


def test_faster_whisper_worker_import_stays_per_row_light() -> None:
    """The one-shot worker must not import the app's LLM stack per audio row."""
    source_root = str(Path(__file__).resolve().parents[2] / "src")
    script = """
import json
import sys
import time
sys.path.insert(0, sys.argv[1])
started = time.perf_counter()
import frisket.engine._workers.faster_whisper_worker  # noqa: F401
elapsed = time.perf_counter() - started
print(json.dumps({
    "elapsed": elapsed,
    "llm": any(name == "frisket.ai.llm" or name.startswith("frisket.ai.llm.") for name in sys.modules),
    "pydantic_ai": any(name == "pydantic_ai" or name.startswith("pydantic_ai.") for name in sys.modules),
}))
"""
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script, source_root],
        check=True,
        capture_output=True,
        text=True,
    )
    measured = json.loads(proc.stdout)
    assert measured["elapsed"] < 1.0, measured
    assert measured["llm"] is False, measured
    assert measured["pydantic_ai"] is False, measured


def test_local_worker_normalizes_empty_words_language_and_missing_duration(
    monkeypatch,
):
    from frisket.engine._workers import faster_whisper_worker as worker

    class FakeWhisperModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            segment = SimpleNamespace(
                start=0.0,
                end=1.25,
                text=" hello ",
                words=[
                    SimpleNamespace(word="   ", start=0.0, end=0.1),
                    SimpleNamespace(word=" hello ", start=0.1, end=1.25),
                ],
            )
            return [segment], SimpleNamespace(language="   ")

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(_local_worker_request("unused.wav", language="fr")),
    )
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    worker.main()

    [result] = json.loads(out.getvalue())["results"]
    assert result["segments"][0]["words"] == [
        {"word": "hello", "start": 0.1, "end": 1.25}
    ]
    assert result["language"] == "fr"
    assert result["duration"] == 1.25


def test_faster_whisper_worker_reports_clean_error_on_model_load_failure(tmp_path):
    """A missing/undownloadable Whisper model must not blow up as an
    uncaught exception inside the faster_whisper_worker module -- that used
    to print a raw Python traceback to stderr (exit code != 0), which
    `_worker_result` then folded into a `RuntimeError` whose `str()` is the
    full traceback; the scratch API further truncated that to 300 chars,
    landing a mid-word-cut traceback in the user-facing error card.
    WhisperModel(...) construction must be guarded the same way Parakeet
    already guards its model-load (onnx_asr.load_model), emitting a clean
    structured JSON error instead.

    This actually EXECUTES the production frisket.engine._workers.faster_whisper_worker
    module as a subprocess with a fake `faster_whisper` module substituted
    ahead of the real one on PYTHONPATH, so it exercises the real guard, not
    a re-implementation of it.
    """
    fake_pkg_dir = tmp_path / "fake_faster_whisper"
    fake_pkg_dir.mkdir()
    (fake_pkg_dir / "faster_whisper.py").write_text(
        "class WhisperModel:\n"
        "    def __init__(self, *a, **kw):\n"
        "        raise RuntimeError(\n"
        "            'could not locate model snapshot; network unreachable'\n"
        "        )\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(fake_pkg_dir), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-m", "frisket.engine._workers.faster_whisper_worker"],
        input=_local_worker_request("unused.wav", model_size="base").encode(),
        capture_output=True,
        env=env,
    )

    # Clean exit (code 0): the failure was caught, not propagated.
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert b"Traceback" not in proc.stderr

    out = json.loads(proc.stdout)
    assert "error" in out
    message = out["error"]["message"]
    assert "Traceback" not in message
    assert "could not locate model snapshot" in message
    # Actionable, like Parakeet's guarded errors: names the engine/model and
    # what to do about it, not just a bare exception repr.
    assert "unavailable" in message
    assert "engine" in message

    # The shared v1 parser translates the clean error envelope into the same
    # short RuntimeError used at the local and gateway consumption seams.
    with pytest.raises(RuntimeError) as excinfo:
        parse_transcription_response(out)
    clean_message = str(excinfo.value)
    assert "Traceback" not in clean_message
    assert len(clean_message) < 300
    assert "could not locate model snapshot" in clean_message


# ---------------------------------------------------------------------------
# faster-whisper 'base' pinned-snapshot resolution (task: complete the
# local-model provisioning story alongside Parakeet). model_size == "base"
# (the default) now resolves through the pinned manifest entry, offline from
# the local Hub cache when present; any other size — or a cache miss — keeps
# today's unpinned lazy WhisperModel(size, ...) resolution unchanged.


def test_resolve_pinned_base_snapshot_uses_the_manifest_pin(monkeypatch, tmp_path):
    from frisket.engine._workers import faster_whisper_worker as worker
    from frisket.ai.models import artifact_manifest

    entry = artifact_manifest.whisper_base_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    snap = entry.hf_snapshot

    calls = []

    def fake_snapshot_download(repo_id, **kwargs):
        calls.append({"repo_id": repo_id, **kwargs})
        return str(tmp_path / "snapshot")

    import huggingface_hub

    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot_download)

    resolved = worker._resolve_pinned_base_snapshot()

    assert resolved == str(tmp_path / "snapshot")
    assert calls == [
        {
            "repo_id": snap.repo_id,
            "revision": snap.revision,
            "allow_patterns": list(snap.files),
            "cache_dir": calls[0]["cache_dir"],  # env-dependent, not asserted
            "local_files_only": True,
            "token": False,
        }
    ]


def test_resolve_pinned_base_snapshot_returns_none_on_cache_miss(monkeypatch):
    """No pinned snapshot cached (this worker's sandbox has no network) --
    the caller falls back to today's unpinned WhisperModel('base', ...)."""
    from frisket.engine._workers import faster_whisper_worker as worker

    import huggingface_hub

    def boom(repo_id, **kwargs):
        raise ValueError("not cached locally")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)

    assert worker._resolve_pinned_base_snapshot() is None


def test_faster_whisper_worker_loads_pinned_snapshot_path_for_base(
    monkeypatch, tmp_path
):
    """When the pin resolves, the worker must pass the resolved LOCAL PATH
    to WhisperModel -- not the bare 'base' string, which would let
    faster_whisper's own library resolve an unpinned revision instead."""
    from frisket.engine._workers import faster_whisper_worker as worker
    from frisket.ai.models import artifact_manifest

    resolved_dir = str(tmp_path / "pinned-snapshot")
    monkeypatch.setattr(worker, "_resolve_pinned_base_snapshot", lambda: resolved_dir)

    captured = {}

    class FakeInfo:
        language = "en"
        duration = 0.0

    class FakeWhisperModel:
        def __init__(self, model_source, **kwargs):
            captured["model_source"] = model_source

        def transcribe(self, *a, **kw):
            return [], FakeInfo()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stdin = _local_worker_request("unused.wav", model_size="base")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    worker.main()

    assert captured["model_source"] == resolved_dir
    result = json.loads(out.getvalue())["results"][0]
    entry = artifact_manifest.whisper_base_artifact()
    assert entry is not None and entry.hf_snapshot is not None
    assert result["model_ids"] == [entry.hf_snapshot.repo_id]
    assert result["revision"] == entry.hf_snapshot.revision
    assert result["warnings"] == []


def test_faster_whisper_worker_keeps_unpinned_resolution_for_other_sizes(
    monkeypatch,
):
    """Only 'base' resolves through the pin -- an explicit non-default
    model_size keeps today's lazy WhisperModel(size, ...) call unchanged."""
    from frisket.engine._workers import faster_whisper_worker as worker

    def fail_if_called():
        raise AssertionError("only 'base' should attempt pinned resolution")

    monkeypatch.setattr(worker, "_resolve_pinned_base_snapshot", fail_if_called)

    captured = {}

    class FakeInfo:
        language = "en"
        duration = 0.0

    class FakeWhisperModel:
        def __init__(self, model_source, **kwargs):
            captured["model_source"] = model_source

        def transcribe(self, *args, **kwargs):
            return [], FakeInfo()

    fake_module = types.ModuleType("faster_whisper")
    fake_module.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_module)

    stdin = _local_worker_request("unused.wav", model_size="large-v3")
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    worker.main()

    assert captured["model_source"] == "large-v3"
    result = json.loads(out.getvalue())["results"][0]
    assert result["model_ids"] == ["faster-whisper/large-v3"]
    assert result["revision"] == "runtime-resolved"


def test_local_faster_whisper_model_call_uses_v1_provenance(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    call = transcribe_engines.transcription_model_calls(
        "faster_whisper",
        str(audio),
        {"model_size": "small", "vad": False},
        {
            "duration": 1.25,
            "model_ids": ["faster-whisper/small"],
            "revision": "runtime-resolved",
            "device": "cpu",
            "dtype": "int8",
            "timings": {"inference_seconds": 0.5},
            "warnings": ["runtime revision"],
            "accepted_options": {"model_size": "small", "vad": False},
        },
        transport="local",
    )[0]

    assert call["provider_kind"] == "local_process"
    assert call["model_ids"] == ["faster-whisper/small"]
    assert call["warnings"] == ["runtime revision"]
    assert call["units"] == {
        "input_bytes": 4,
        "audio_seconds": 1.25,
        "model_revision": "runtime-resolved",
        "device": "cpu",
        "dtype": "int8",
        "timing_inference_seconds": 0.5,
        "option_model_size": "small",
        "option_vad": False,
    }


# ---------------------------------------------------------------------------
# remote engine (OpenAI-dialect /audio/transcriptions, bespoke — not router)


def test_remote_engine_needs_configured_provider(tmp_path, monkeypatch):
    """engine='openai/whisper-1' without provider keys fails with a pointer
    to the local engines — never a silent fallback."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    router = ModelRouter(cache=None, cache_mode="off")
    ctx = OpContext(http=httpx.AsyncClient(), extras={"router": router})
    path = _wav(tmp_path / "a.wav")
    with pytest.raises(RuntimeError, match="'openai' provider"):
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "openai/whisper-1", str(path), {}, ctx, transport="remote"
            )
        )


def test_remote_engine_posts_audio(tmp_path, monkeypatch):
    """engine='openai/whisper-1' POSTs the audio bytes to the provider's
    /audio/transcriptions with the router's credentials; segments and
    language come back in the whisper-engine shape."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    router = ModelRouter(cache=None, cache_mode="off")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.update(
            url=str(request.url),
            auth=request.headers.get("authorization"),
            sent_audio=b"RIFF" in body,
            sent_model=b"whisper-1" in body,
        )
        return httpx.Response(
            200,
            json={
                "text": " hello world ",
                "language": "en",
                "duration": 0.5,
                "segments": [{"start": 0.0, "end": 0.5, "text": " hello world "}],
            },
        )

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    path = _wav(tmp_path / "a.wav")
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "openai/whisper-1", str(path), {}, ctx, transport="remote"
        )
    )
    assert seen["url"].endswith("/audio/transcriptions")
    assert seen["auth"] == "Bearer sk-test"
    assert seen["sent_audio"] and seen["sent_model"]
    assert result.output["text"] == "hello world"
    assert result.output["segments"] == [
        {"start": 0.0, "end": 0.5, "text": "hello world", "segment_index": 0}
    ]
    assert result.output["language"] == "en"
    # the (data, meta) cost channel: 0.5s of whisper-1 at the per-second rate
    # from pricing_data.json must bill a nonzero cost_actual instead of the
    # $0 it reported before
    rate = audio_price("whisper-1")["per_second"]
    assert rate > 0
    assert result.output["cost"] == pytest.approx(0.5 * rate)


def test_mai_transcribe_2_posts_its_documented_openrouter_options(tmp_path):
    """MAI is an exact OpenRouter profile, never a widening of remote defaults."""
    router = ModelRouter(
        keys={"openrouter": "or-test"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(
            url=str(request.url),
            auth=request.headers.get("authorization"),
            body=body,
        )
        return httpx.Response(
            200,
            json={
                "text": "hello from speaker zero",
                "language": "en",
                "duration": 1.25,
                "segments": [
                    {
                        "start": 0,
                        "end": 1.25,
                        "text": " hello from speaker zero ",
                        "speaker": 0,
                    }
                ],
            },
        )

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "openrouter/microsoft/mai-transcribe-2",
            str(_wav(tmp_path / "mai.wav")),
            {
                "engine": "openrouter/microsoft/mai-transcribe-2",
                "language": ["en"],
                "diarize": False,
                "clean": True,
                "context": " Frisket, Jane Smith, , CTranslate2 ",
            },
            ctx,
            transport="remote",
        )
    )

    body = seen["body"]
    assert isinstance(body, dict)
    assert seen["url"] == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer or-test"
    assert body["model"] == "microsoft/mai-transcribe-2"
    assert body["response_format"] == "verbose_json"
    assert body["timestamp_granularities"] == ["segment"]
    assert body["input_audio"]["format"] == "wav"
    assert body["provider"] == {
        "options": {
            "azure": {
                "diarization": {"enabled": False},
                "phraseList": {"phrases": ["Frisket", "Jane Smith", "CTranslate2"]},
                "enhancedMode": {"modelOptions": {"transcribeStyle": "clean"}},
            }
        }
    }
    assert result.output["segments"] == [
        {
            "start": 0.0,
            "end": 1.25,
            "text": "hello from speaker zero",
            "speaker": "0",
            "segment_index": 0,
        }
    ]
    assert result.output["cost"] == pytest.approx(round(1.25 * (0.1 / 3600), 6))


def test_mai_transcribe_2_rejects_unsupported_format_before_egress(tmp_path):
    router = ModelRouter(
        keys={"openrouter": "or-test"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("unsupported MAI audio format reached OpenRouter")

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    unsupported = _wav(tmp_path / "mai.ogg")
    with pytest.raises(ValueError, match="MAI-Transcribe 2 accepts WAV, MP3, or FLAC"):
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "openrouter/microsoft/mai-transcribe-2",
                str(unsupported),
                {},
                ctx,
                transport="remote",
            )
        )


def test_declared_openrouter_model_does_not_inherit_mai_request_options(
    tmp_path, monkeypatch
):
    declaration = EngineDeclaration(
        id="openrouter/example-transcribe",
        label="Example transcription",
        tier="hosted",
        provider="openrouter",
        transcription=TranscriptionEngineCapabilities(
            transport="remote", language_mode="single", detects_language=False
        ),
    )
    monkeypatch.setattr(
        media,
        "TRANSCRIBE_ENGINE_TABLE",
        (*media.TRANSCRIBE_ENGINE_TABLE, declaration),
    )
    router = ModelRouter(
        keys={"openrouter": "or-test"},
        cache=None,
        cache_mode="off",
        use_env_keys=False,
    )
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["content-type"]
        seen["body"] = request.content.decode("latin-1")
        return httpx.Response(200, json={"text": "hello", "segments": []})

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    asyncio.run(
        transcribe_engines.OpenAITranscriptionAdapter().transcribe(
            declaration.id,
            str(_wav(tmp_path / "example.wav")),
            {},
            ctx,
            {"filename": "example.wav", "mime": "audio/wav"},
        )
    )

    assert seen["content_type"].startswith("multipart/form-data")
    assert '"provider"' not in seen["body"]
    assert "azure" not in seen["body"]


def test_remote_engine_routes_qualified_local_model_and_records_local_facts(
    tmp_path,
) -> None:
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    secret = "local-transcribe-token"
    router = ModelRouter(
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="studio",
                display_name="Studio",
                origin="https://studio.example.test",
                source="local_file",
                inference_token=secret,
            ),
        ),
    )
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen.update(
            url=str(request.url),
            auth=request.headers.get("authorization"),
            bare_model=b"whisper-1" in body,
            qualified_model=b"@studio" in body,
        )
        return httpx.Response(
            200,
            json={
                "text": "local transcript",
                "language": "en",
                "segments": [],
            },
        )

    # This bare name has a nonzero hosted price. The canonical local identity
    # must win before provider stripping, and zero needs no duration estimate.
    engine = "ollama/@studio/whisper-1"
    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            engine, str(_wav(tmp_path / "a.wav")), {}, ctx, transport="remote"
        )
    )

    assert seen == {
        "url": "https://studio.example.test/v1/audio/transcriptions",
        "auth": f"Bearer {secret}",
        "bare_model": True,
        "qualified_model": False,
    }
    assert result.output["text"] == "local transcript"
    call = result.model_calls[0]
    assert call["engine"] == engine
    assert call["provider"] == "ollama"
    assert call["provider_kind"] == "local_http"
    assert call["model_ids"] == ["whisper-1"]
    assert call["provider_cost_usd"] == 0.0
    assert call["cost_source"] == "free_local"
    assert call["warnings"] == []
    assert result.output["cost"] == 0.0


def test_qualified_local_http_failure_still_records_known_zero(tmp_path) -> None:
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    router = ModelRouter(
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="studio",
                display_name="Studio",
                origin="https://studio.example.test",
                source="local_file",
                inference_token="local-token",
            ),
        ),
    )
    ctx = OpContext(
        http=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(503))
        ),
        extras={"router": router},
    )
    engine = "ollama/@studio/frisket-new-unlisted-model"

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(
            transcribe_engines.OpenAITranscriptionAdapter().transcribe(
                engine, str(_wav(tmp_path / "a.wav")), {}, ctx, None
            )
        )

    accounting = getattr(raised.value, "accounting")
    assert accounting["cost"] == 0.0
    call = accounting["model_calls"][0]
    assert call["engine"] == engine
    assert call["provider_cost_usd"] == 0.0
    assert call["cost_source"] == "free_local"
    assert call["warnings"] == []


def _multipart_filename(body: bytes) -> str | None:
    """The `filename="..."` of the part named `file`, off the raw wire body."""
    import re

    text = body.decode("utf-8", errors="ignore")
    match = re.search(r'name="file";\s*filename="([^"]*)"', text)
    return match.group(1) if match else None


def test_remote_engine_names_the_upload_from_the_media_row(tmp_path, monkeypatch):
    """whisper-1 answered "Unrecognized file format" on a standard PCM WAV.

    A blob-backed media row materializes at its CONTENT-ADDRESSED path --
    `<root>/e3/e3b0c442...`, a bare 64-hex digest with no extension -- and the
    upload took its multipart filename from that basename. whisper-class
    endpoints sniff the container format from the extension, so a nameless
    part is an unrecognized file however valid the bytes are.

    The existing `test_remote_engine_posts_audio` above cannot see this: it
    passes a raw local `a.wav` PATH, so the basename is already correct. The
    defect only appears on the blob cell -- the shape the durable path
    actually runs.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    router = ModelRouter(cache=None, cache_mode="off")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read()
        seen["filename"] = _multipart_filename(body)
        seen["sent_audio"] = b"RIFF" in body
        return httpx.Response(
            200, json={"text": "hi", "language": "en", "duration": 0.5, "segments": []}
        )

    from frisket.engine.store import Project
    from frisket.engine.store.media_blobs import MediaBlobStore

    project = Project.create(tmp_path / "p.frisket", name="P")
    try:
        digest = project.add_blob(
            _wav(tmp_path / "interview.wav").read_bytes(),
            filename="interview.wav",
            mime="audio/wav",
        )
        cell = MediaBlobStore(project).media_cell(
            digest, mime="audio/wav", filename="interview.wav"
        )
        # The path really is the extensionless digest -- the precondition the
        # defect turned on.
        with project.materialize_blob(digest) as materialized:
            assert Path(materialized).suffix == ""

        ctx = OpContext(
            project=project,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            extras={"router": router},
        )
        with project.materialize_blob(digest) as path:
            asyncio.run(
                transcribe_engines.run_transcription_engine(
                    "openai/whisper-1",
                    str(path),
                    {},
                    ctx,
                    transport="remote",
                    media=cell,
                )
            )
    finally:
        project.close()

    assert seen["sent_audio"] is True
    assert seen["filename"] == "interview.wav"


def test_remote_engine_upload_suffix_follows_the_media_mime(tmp_path, monkeypatch):
    """The suffix is never hardcoded `.wav`: announcing an mp3 as a WAV just
    trades an unrecognized-format error for a decode error further in. A cell
    with no filename still gets an honest extension, derived from its mime."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    router = ModelRouter(cache=None, cache_mode="off")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["filename"] = _multipart_filename(request.read())
        seen["content_type"] = "audio/mpeg" in request.read().decode(
            "utf-8", errors="ignore"
        )
        return httpx.Response(
            200, json={"text": "hi", "language": "en", "duration": 0.5, "segments": []}
        )

    from frisket.engine.store import Project
    from frisket.engine.store.media_blobs import MediaBlobStore

    project = Project.create(tmp_path / "p.frisket", name="P")
    try:
        digest = project.add_blob(b"ID3fake-mp3-bytes", mime="audio/mpeg")
        cell = MediaBlobStore(project).media_cell(digest, mime="audio/mpeg")
        ctx = OpContext(
            project=project,
            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            extras={"router": router},
        )
        with project.materialize_blob(digest) as path:
            asyncio.run(
                transcribe_engines.run_transcription_engine(
                    "openai/whisper-1",
                    str(path),
                    {},
                    ctx,
                    transport="remote",
                    media=cell,
                )
            )
    finally:
        project.close()

    assert seen["filename"] == "audio.mp3"
    # ...and the part declares the row's own container type, not a blanket
    # application/octet-stream.
    assert seen["content_type"] is True


def test_remote_engine_ollama_edge_auth_raises_typed_unsupported_error(
    tmp_path,
) -> None:
    """The split-host Caddy front door does not allowlist
    ``POST /v1/audio/transcriptions``; borrowing the Ollama
    adapter's credentials for that route would just 403 opaquely through an
    authenticated front door. A typed, honest error instead of a generic
    transport failure."""
    from frisket.sdk.ops.transcribe_engines import LocalTranscribeUnsupportedError
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    router = ModelRouter(
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="heavy",
                display_name="Heavy local server",
                origin="https://llm.heavy.internal",
                inference_token="secret",
                edge_auth=True,
                source="env",
            ),
        ),
    )
    ctx = OpContext(http=httpx.AsyncClient(), extras={"router": router})
    path = _wav(tmp_path / "a.wav")
    with pytest.raises(
        LocalTranscribeUnsupportedError, match="authenticated front door"
    ):
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "ollama/@heavy/qwen3:8b", str(path), {}, ctx, transport="remote"
            )
        )


def test_remote_engine_ollama_tokenless_keeps_todays_behavior(tmp_path) -> None:
    """A direct/tokenless local deployment (edge_auth False, the default) is
    unaffected -- it still attempts the borrowed request exactly as before
    this stage (and gets a normal transport-level failure, not the typed
    unsupported error, since nothing here is authenticated)."""
    from frisket.ai.llm.endpoint_config import LocalModelEndpointConfig

    router = ModelRouter(
        cache=None,
        cache_mode="off",
        local_endpoints=(
            LocalModelEndpointConfig(
                endpoint_id="desktop",
                display_name="Desktop local server",
                origin="http://127.0.0.1:11434",
                source="local_file",
            ),
        ),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    path = _wav(tmp_path / "a.wav")
    from frisket.sdk.ops.transcribe_engines import LocalTranscribeUnsupportedError

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            transcribe_engines.run_transcription_engine(
                "ollama/@desktop/qwen3:8b", str(path), {}, ctx, transport="remote"
            )
        )
    assert not isinstance(excinfo.value, LocalTranscribeUnsupportedError)
    assert "remote transcription failed" in str(excinfo.value)


def test_remote_engine_failure_redacts_borrowed_provider_key(tmp_path) -> None:
    secret = "checkpoint1b-transcribe-secret"
    router = ModelRouter(
        keys={"openai": secret}, cache=None, cache_mode="off", use_env_keys=False
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        return httpx.Response(500, text=f"provider echoed {secret}")

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            transcribe_engines.OpenAITranscriptionAdapter().transcribe(
                "openai/whisper-1", str(_wav(tmp_path / "a.wav")), {}, ctx, None
            )
        )
    assert secret not in str(excinfo.value)
    assert "remote transcription failed (500)" in str(excinfo.value)


@pytest.mark.parametrize("mode", ["transport", "json", "postprocess"])
def test_remote_engine_normalizes_all_success_path_failures(
    tmp_path, mode: str
) -> None:
    secret = "checkpoint1b-remote-normalize-secret"
    router = ModelRouter(
        keys={"openai": secret}, cache=None, cache_mode="off", use_env_keys=False
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "transport":
            raise httpx.ConnectError(f"echoed {secret}", request=request)
        if mode == "json":
            return httpx.Response(200, content=f"not json {secret}")
        return httpx.Response(
            200, json={"segments": [{"start": "not-a-number", "end": 1, "text": "x"}]}
        )

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            transcribe_engines.OpenAITranscriptionAdapter().transcribe(
                "openai/whisper-1", str(_wav(tmp_path / "a.wav")), {}, ctx, None
            )
        )
    assert secret not in str(excinfo.value)


def test_remote_engine_survives_hostile_transport_stringification(tmp_path) -> None:
    secret = "checkpoint1b-hostile-transcribe-transport-secret"
    router = ModelRouter(
        keys={"openai": secret}, cache=None, cache_mode="off", use_env_keys=False
    )

    class HostileConnectError(httpx.ConnectError):
        def __str__(self) -> str:
            raise RuntimeError(secret)

    def handler(request: httpx.Request) -> httpx.Response:
        raise HostileConnectError("unused", request=request)

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            transcribe_engines.OpenAITranscriptionAdapter().transcribe(
                "openai/whisper-1", str(_wav(tmp_path / "a.wav")), {}, ctx, None
            )
        )
    assert secret not in str(excinfo.value)
    assert "remote transcription transport error" in str(excinfo.value)


def test_remote_engine_survives_hostile_postprocess_stringification(
    tmp_path, monkeypatch
) -> None:
    secret = "checkpoint1b-hostile-transcribe-postprocess-secret"
    router = ModelRouter(
        keys={"openai": secret}, cache=None, cache_mode="off", use_env_keys=False
    )

    class HostileValueError(ValueError):
        def __str__(self) -> str:
            raise RuntimeError(secret)

    def fail_postprocess(segment):  # noqa: ANN001
        raise HostileValueError("unused")

    monkeypatch.setattr(
        "frisket.sdk.ops.transcription.hosted._segment_with_speaker", fail_postprocess
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"segments": [{"start": 0, "end": 1, "text": "x"}]}
        )

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            transcribe_engines.OpenAITranscriptionAdapter().transcribe(
                "openai/whisper-1", str(_wav(tmp_path / "a.wav")), {}, ctx, None
            )
        )
    assert secret not in str(excinfo.value)
    assert "malformed remote transcription response" in str(excinfo.value)


def test_remote_engine_token_priced_model_costs_from_usage(tmp_path, monkeypatch):
    """gpt-4o-transcribe-class models bill per token: cost comes from the
    response's usage block times the rates in pricing_data.json."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    router = ModelRouter(cache=None, cache_mode="off")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "text": "hello",
                "usage": {"type": "tokens", "input_tokens": 100, "output_tokens": 10},
            },
        )

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    path = _wav(tmp_path / "a.wav")
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "openai/gpt-4o-transcribe", str(path), {}, ctx, transport="remote"
        )
    )
    price = audio_price("gpt-4o-transcribe")
    assert result.output["cost"] == pytest.approx(
        100 * price["input_per_token"] + 10 * price["output_per_token"]
    )


def test_remote_engine_unknown_model_reports_unknown_cost(tmp_path, monkeypatch):
    """A model absent from pricing_data.json bills cost=None — 'price
    unknown', not a fabricated estimate and not a false $0."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    router = ModelRouter(cache=None, cache_mode="off")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "hello", "duration": 60.0})

    ctx = OpContext(
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        extras={"router": router},
    )
    path = _wav(tmp_path / "a.wav")
    result = asyncio.run(
        transcribe_engines.run_transcription_engine(
            "openai/acme-stt-9000", str(path), {}, ctx, transport="remote"
        )
    )
    assert result.output["cost"] is None


# ---------------------------------------------------------------------------
# parakeet engine (onnx-asr, local — needs the cached onnx model)

needs_parakeet = pytest.mark.skipif(
    not _parakeet_cached(), reason="parakeet-tdt onnx model not in the HF cache"
)


@needs_parakeet
def test_parakeet_engine_runs_locally(tmp_path):
    """The real onnx model loads (int8) and runs offline in the sandboxed
    worker. Silence may hallucinate a filler word (ASR artifact), so pin the
    output CONTRACT: text is a string and the word segments reassemble it."""
    pytest.importorskip("onnx_asr")
    path = _wav(tmp_path / "silence.wav")
    out = asyncio.run(transcribe_engines.ParakeetAdapter().transcribe(str(path), {}))
    assert isinstance(out["text"], str)
    assert isinstance(out["segments"], list)
    for seg in out["segments"]:
        assert {"start", "end", "text"} <= set(seg)
    assert " ".join(s["text"] for s in out["segments"]) == out["text"]


@needs_parakeet
@pytest.mark.skipif(
    shutil.which("say") is None, reason="speech synthesis needs macOS `say`"
)
def test_parakeet_transcribes_speech_end_to_end(tmp_path):
    """Full runner path: spec.engine='parakeet-tdt' switches the backend and the
    transcript + word-level timestamped segments land in the sheet."""
    pytest.importorskip("onnx_asr")
    from frisket.engine.runner import MapRunner
    from frisket.engine.store import Project

    wav = tmp_path / "speech.wav"
    subprocess.run(
        [
            "say",
            "-o",
            str(wav),
            "--data-format=LEI16@16000",
            "the quick brown fox jumps over the lazy dog",
        ],
        check=True,
    )

    p = Project.create(tmp_path / "t.frisket", name="t")
    sheet = p.add_sheet("data")
    cols = {"audio": p.add_column(sheet, "audio", type="audio")}
    p.add_rows(sheet, [{"audio": str(wav)}], cols)
    plan = _transcribe_plan(p, sheet, engine="parakeet-tdt")

    def run(plan):
        return run_typed_map_rows_action(
            p,
            "test",
            BoundTypedActionRequest.bind(plan.action, plan.request),
            None,
            lambda project, router: MapRunner(
                project,
                router or ModelRouter(cache=None, cache_mode="off"),
                authority=open_attempt_authority(project),
            ),
        )

    result = run(plan)
    assert result.status == "completed", result.errors

    def col_value(name):
        col = next(c for c in p.columns(sheet) if c["name"] == name)
        (val,) = p.get_values(sheet, col["id"]).values()
        return val

    assert "quick brown fox" in col_value("transcript").lower()
    # VAD on (default): segments are utterance-level {start,end,text} —
    # silence yields none (no hallucinated text on dead air)
    segs = col_value("transcript_segments")
    assert len(segs) >= 1 and all({"start", "end", "text"} <= set(s) for s in segs)
    assert segs[0]["start"] >= 0.0 and segs[-1]["end"] > segs[0]["start"]
    # Parakeet does not report a detected language on this API, so no
    # detected_language column may be created for it
    assert "detected_language" not in {c["name"] for c in p.columns(sheet)}

    # vad=false trades hallucination-resistance for WORD-level timestamps
    plan = _transcribe_plan(p, sheet, engine="parakeet-tdt", vad=False, output="raw")
    result = run(plan)
    assert result.status == "completed", result.errors
    raw_segs = col_value("raw_segments")
    assert len(raw_segs) >= 5, "vad=false should yield word-level segments"
    p.close()


def test_path_cells_rejected_on_untrusted_deploys(monkeypatch, tmp_path):
    """Hosted (FRISKET_ALLOW_CODE_RECIPES=0): a text cell naming a server
    filesystem path must NOT be readable — transcribe was the one media op
    missing the gate its siblings (ocr/convert/media) have. Blobs still work."""
    from frisket.ops.base import materialize_media_path

    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "0")
    with pytest.raises(ValueError, match="ingested blob"):
        with materialize_media_path("/etc/passwd", ctx=None, op="transcribe"):
            pass
    # trusted/local default still allows paths
    monkeypatch.setenv("FRISKET_ALLOW_CODE_RECIPES", "1")
    with materialize_media_path("/tmp/x.mp3", ctx=None, op="transcribe") as path:
        assert path == Path("/tmp/x.mp3")


# ---------------------------------------------------------------------------
# engine-table parity: ops <-> contract <-> catalog all consume
# TRANSCRIBE_ENGINE_TABLE — pin the wiring (the drift class the OCR family
# hit: implemented + catalog-advertised engines the contract rejected).


def test_ops_engine_roster_equals_the_contract_symbolic_set():
    from frisket.contracts.action import TRANSCRIBE_SYMBOLIC_ENGINES
    from frisket.sdk.ops.transcribe_engines import ENGINE_ALIASES, ENGINE_CHOICES

    assert set(ENGINE_CHOICES) | set(ENGINE_ALIASES) == TRANSCRIBE_SYMBOLIC_ENGINES


def test_every_symbolic_engine_validates_through_the_params_model():
    from frisket.contracts.action import TRANSCRIBE_SYMBOLIC_ENGINES

    for engine in sorted(TRANSCRIBE_SYMBOLIC_ENGINES):
        params = TranscribeParams.model_validate({"source": "audio", "engine": engine})
        assert params.engine.root == engine


def test_catalog_roster_carries_every_listed_table_engine_with_its_label():
    from frisket.contracts.actions.schemas._engines import TRANSCRIBE_ENGINE_TABLE
    from frisket.server.action_catalog_hints import (
        project_action_catalog_launcher_hints,
    )

    hints = project_action_catalog_launcher_hints(
        {"configured": False, "available": False, "engines": [], "error": None}
    )
    by_id = {e["id"]: e for e in hints["media.transcribe"]["engines"]}
    for decl in TRANSCRIBE_ENGINE_TABLE:
        if not decl.listed:
            continue
        assert decl.id in by_id, f"table engine {decl.id!r} missing from catalog"
        assert by_id[decl.id]["label"] == decl.label


def test_alias_maps_agree_and_carry_no_resolves_to_oddity():
    """The engine-roster cutover deleted the `remote` row, so no engine id canonicalizes to a
    provider/model id anymore: the ops alias map and the contract alias map
    are the SAME advertised-alias mapping."""
    from frisket.contracts.actions.schemas.media import TRANSCRIBE_ENGINE_ALIASES
    from frisket.sdk.ops.transcribe_engines import ENGINE_ALIASES

    assert "remote" not in ENGINE_ALIASES
    assert "remote" not in TRANSCRIBE_ENGINE_ALIASES
    assert ENGINE_ALIASES == TRANSCRIBE_ENGINE_ALIASES
