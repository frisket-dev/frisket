from __future__ import annotations

import asyncio
import json
import io
import shutil
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
import httpx
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store.receipts import ReceiptStore
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.server.app import create_app
from frisket.sdk.ops import transcribe_engines


WAV = b"RIFF0000WAVEfmt paid scratch transcript"


def _client(tmp_path, monkeypatch, *, duration=15.0):
    monkeypatch.setattr(
        "frisket.server.services.scratch_transcribe.probe_for_ingest",
        lambda *args, **kwargs: {"kind": "audio", "duration_seconds": duration},
    )
    router = ModelRouter(keys={"openai": "test-key"}, cache=None, cache_mode="off")
    client = TestClient(
        create_app(
            tmp_path / "ws",
            router=router,
            executor_deps_factory=lambda _project_id, _request: ExecutorDeps(
                consent_coverage=ConsentCoverage("test:scratch", Decimal("0"))
            ),
        )
    )
    pid = client.post("/api/projects", json={"name": "Scratch ASR"}).json()["id"]
    return client, pid


def _multipart(payload):
    return {
        "files": {"file": ("sample.wav", WAV, "audio/wav")},
        "data": {"payload": json.dumps(payload)},
    }


def _wait(client, pid, preview_id):
    for _ in range(200):
        response = client.get(f"/api/projects/{pid}/actions/v1/preview/{preview_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] != "running":
            return body
        time.sleep(0.01)  # realtime: yield while the actual preview thread finishes
    raise AssertionError("scratch transcription preview did not finish")


@pytest.mark.parametrize(
    "cancel_after_provider_acceptance",
    [False, True],
    ids=("completed", "cancelled-after-provider-acceptance"),
)
def test_scratch_transcription_default_limit_quote_and_paid_receipt(
    tmp_path, monkeypatch, cancel_after_provider_acceptance
):
    client, pid = _client(tmp_path, monkeypatch)
    project = client.app.state.workspace.get(pid)
    settled = []

    class Settlement:
        def settle_action_receipt(self, *, project, project_id, receipt_id):
            receipt = ReceiptStore(project).parsed_by_id(receipt_id)
            assert project_id == pid
            assert receipt.status == (
                "cancelled" if cancel_after_provider_acceptance else "completed"
            )
            settled.append(receipt_id)

    client.app.state.workspace.direct_action_receipt_settlement_port = Settlement()
    untouched = {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("rows", "columns", "blobs", "runs", "results")
    }
    calls = []
    entered = threading.Event()
    release = threading.Event()

    async def transcribe(self, engine, path, spec, ctx, media):
        del self, path, spec, media
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        calls.append(engine)
        return {
            "text": "hello world",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.25,
                    "text": "hello world",
                    "speaker": "SPEAKER_00",
                    "words": [{"word": "hello", "start": 0.0, "end": 0.5}],
                }
            ],
            "language": "en",
            "duration": 15.0,
            "cost": 0.01,
            "cost_source": "pricing_data",
            "credential_source": ctx.extras["router"].credential_source_for("openai"),
        }

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter, "transcribe", transcribe
    )
    request = {"engine": "openai/whisper-1"}
    quote = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(request),
    )
    assert quote.status_code == 200, quote.text
    quoted = quote.json()
    assert quoted["source"]["time_limit_seconds"] == 600
    assert quoted["source"]["duration_seconds"] == 15
    assert quoted["source"]["effective_duration_seconds"] == 15
    assert quoted["estimate"]["audio_seconds"] == 15
    assert quoted["estimate"]["requires_confirmation"] is True
    token = quoted["estimate"]["promise_set_hash"]

    refused = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart({**request, "confirmation": "wrong"}),
    )
    assert refused.status_code == 402
    assert not calls and ReceiptStore(project).count() == 0

    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart({**request, "confirmation": token}),
    )
    assert started.status_code == 202, started.text
    preview_id = started.json()["preview_id"]
    assert entered.wait(5)
    if cancel_after_provider_acceptance:
        [attempt_id] = project.db.execute(
            "SELECT id FROM execution_attempts"
        ).fetchone()
        project.db.execute(
            "INSERT INTO attempt_row_authorizations "
            "(attempt_id,row_id,quoted_quantity) VALUES (?,?,?)",
            (attempt_id, 1, "0.25"),
        )
        project.db.commit()
        assert (
            client.delete(
                f"/api/projects/{pid}/actions/v1/preview/{preview_id}"
            ).status_code
            == 204
        )
    release.set()
    result = _wait(client, pid, preview_id)
    assert result["status"] == (
        "cancelled" if cancel_after_provider_acceptance else "done"
    ), result
    if not cancel_after_provider_acceptance:
        row = result["result"]["rows"][0]
        assert row["text"]["value"] == "hello world"
        assert row["segments"]["value"][0]["segment_index"] == 0
        assert row["segments"]["value"][0]["speaker"] == "SPEAKER_00"
        assert row["segments"]["value"][0]["words"][0]["word"] == "hello"
    assert result["accounting"]["model_call_count"] == 1
    assert result["accounting"]["cost_actual"] == pytest.approx(0.01)
    receipt = ReceiptStore(project).parsed_by_id(result["accounting"]["receipt_id"])
    assert receipt.run_id is None and receipt.status == (
        "cancelled" if cancel_after_provider_acceptance else "completed"
    )
    assert settled == [receipt.receipt_id]
    assert project.db.execute(
        "SELECT state FROM execution_attempts WHERE receipt_id=?",
        (receipt.receipt_id,),
    ).fetchone()[0] == ("halted" if cancel_after_provider_acceptance else "effected")
    if cancel_after_provider_acceptance:
        assert (
            project.db.execute(
                "SELECT terminal_outcome FROM attempt_row_authorizations"
            ).fetchone()[0]
            == "succeeded"
        )
    assert {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in untouched
    } == untouched


@pytest.mark.parametrize("limit", [0, -1, float("inf"), "600"])
def test_scratch_transcription_refuses_invalid_limit(tmp_path, monkeypatch, limit):
    client, pid = _client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart({"engine": "faster_whisper", "time_limit_seconds": limit}),
    )
    assert response.status_code == 400
    assert response.json()["field"] == "time_limit_seconds"


@pytest.mark.parametrize(
    "payload",
    ["not-json", json.dumps(["not", "an", "object"])],
)
def test_scratch_transcription_estimate_refuses_malformed_payload(
    tmp_path, monkeypatch, payload
):
    client, pid = _client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        files={"file": ("sample.wav", WAV, "audio/wav")},
        data={"payload": payload},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_params"


def test_scratch_transcription_estimate_bounds_upload_before_buffering(
    tmp_path, monkeypatch
):
    client, pid = _client(tmp_path, monkeypatch)
    monkeypatch.setattr("frisket.server.routes.previews._SCRATCH_MAX_UPLOAD_BYTES", 4)
    response = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart({"engine": "faster_whisper"}),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_input_ref"


def test_scratch_transcription_estimate_keeps_event_loop_responsive(
    tmp_path, monkeypatch
):
    client, pid = _client(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original = client.app.state.workspace.get

    def blocking_get(project_id):
        entered.set()
        assert release.wait(10)
        return original(project_id)

    monkeypatch.setattr(client.app.state.workspace, "get", blocking_get)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            client.post,
            f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
            **_multipart({"engine": "faster_whisper"}),
        )
        assert entered.wait(10)
        health = client.get("/api/health")
        release.set()
        assert pending.result(timeout=10).status_code == 200
    assert health.status_code == 200


@pytest.mark.parametrize("sample_seconds", [120, 121])
def test_scratch_transcription_custom_limit_clips_same_prefix(
    tmp_path, monkeypatch, sample_seconds
):
    from dataclasses import replace

    from frisket.execution.provider import ExecutionLimits

    client, pid = _client(tmp_path, monkeypatch, duration=900.0)
    workspace = client.app.state.workspace
    original_composition = workspace.execution_composition_for
    monkeypatch.setattr(
        workspace,
        "execution_composition_for",
        lambda *args, **kwargs: replace(
            original_composition(*args, **kwargs),
            limits=ExecutionLimits(max_media_seconds=120),
        ),
    )
    cuts = []
    calls = []

    async def cut(_source, *, source_path, start_ms, end_ms, out_path):
        cuts.append((start_ms, end_ms))
        out_path.write_bytes(source_path.read_bytes())

    monkeypatch.setattr("frisket.engine.store.media_clip.cut_clip", cut)

    async def transcribe(self, path, spec, *, should_cancel=None):
        del self, path, spec, should_cancel
        calls.append(True)
        return {"text": "prefix", "segments": [], "language": "en"}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", transcribe
    )
    payload = {"engine": "faster_whisper", "time_limit_seconds": sample_seconds}
    quote = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(payload),
    )
    assert quote.status_code == 200, quote.text
    assert quote.json()["source"]["effective_duration_seconds"] == sample_seconds
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch", **_multipart(payload)
    )
    assert started.status_code == 202, started.text
    result = _wait(client, pid, started.json()["preview_id"])
    if sample_seconds > 120:
        assert result["status"] == "error", result
        assert "max_media_seconds" in str(result["error"])
        assert cuts == [] and calls == []
        return
    assert result["status"] == "done", result
    assert cuts == [(0, 120_000)]
    assert calls == [True]


@pytest.mark.skipif(
    not shutil.which("ffprobe") or not shutil.which("ffmpeg"),
    reason="Media tools are optional",
)
def test_scratch_transcription_probes_and_cuts_real_uploaded_audio(
    tmp_path, monkeypatch
):
    audio = io.BytesIO()
    with wave.open(audio, "wb") as wav:
        wav.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 32_000)
    observed_seconds = []

    async def transcribe(self, path, spec, *, should_cancel=None):
        with wave.open(path, "rb") as wav:
            observed_seconds.append(wav.getnframes() / wav.getframerate())
        return {"text": "bounded", "segments": [], "language": "en"}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", transcribe
    )
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Real clip"}).json()["id"]
        request = {
            "files": {"file": ("sample.wav", audio.getvalue(), "audio/wav")},
            "data": {
                "payload": json.dumps(
                    {"engine": "faster_whisper", "time_limit_seconds": 0.5}
                )
            },
        }
        quote = client.post(
            f"/api/projects/{pid}/transcribe/compare-scratch/estimate", **request
        )
        assert quote.status_code == 200, quote.text
        assert quote.json()["source"]["duration_seconds"] == pytest.approx(2)
        assert quote.json()["source"]["effective_duration_seconds"] == 0.5
        start = client.post(
            f"/api/projects/{pid}/transcribe/compare-scratch", **request
        )
        assert start.status_code == 202, start.text
        result = _wait(client, pid, start.json()["preview_id"])
        assert result["status"] == "done", result
    assert observed_seconds == [pytest.approx(0.5, abs=0.05)]


def test_scratch_transcription_forwards_declared_engine_options(tmp_path, monkeypatch):
    client, pid = _client(tmp_path, monkeypatch)
    captured = []

    async def transcribe(self, path, spec, *, should_cancel=None):
        del self, path, should_cancel
        captured.append(dict(spec))
        return {"text": "options", "segments": [], "language": "eng"}

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", transcribe
    )
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart(
            {
                "engine": "faster_whisper",
                "language": "en",
                "model_size": "small",
                "vad": False,
            }
        ),
    )
    assert started.status_code == 202, started.text
    assert _wait(client, pid, started.json()["preview_id"])["status"] == "done"
    assert captured == [
        {
            "engine": "faster_whisper",
            "language": ["en"],
            "model_size": "small",
            "vad": False,
        }
    ]


@pytest.mark.parametrize("engine", ["faster-whisper", "remote"])
def test_scratch_transcription_refuses_retired_engine_spellings(
    tmp_path, monkeypatch, engine
):
    client, pid = _client(tmp_path, monkeypatch)
    response = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart({"engine": engine}),
    )
    assert response.status_code == 400
    assert "retired" in response.json()["message"]


def test_scratch_transcription_preserves_a_safe_per_candidate_error(
    tmp_path, monkeypatch
):
    client, pid = _client(tmp_path, monkeypatch)
    secret = "sk-scratch-transcribe-secret-1234"

    async def transcribe(self, path, spec, *, should_cancel=None):
        del self, path, spec, should_cancel
        raise RuntimeError(f"model unavailable; api_key={secret}")

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", transcribe
    )
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart({"engine": "faster_whisper"}),
    )
    assert started.status_code == 202, started.text
    result = _wait(client, pid, started.json()["preview_id"])
    assert result["status"] == "error"
    assert result["error"]["code"] == "preview_failed"
    assert "model unavailable" in result["error"]["message"]
    assert secret not in json.dumps(result)
    assert "[REDACTED]" in result["error"]["message"]


def test_scratch_transcription_preserves_returned_remote_failure_accounting(
    tmp_path, monkeypatch
):
    client, pid = _client(tmp_path, monkeypatch)
    project = client.app.state.workspace.get(pid)
    settled = []

    async def failed_response(self, url, **kwargs):
        del self, kwargs
        return httpx.Response(
            503,
            text="provider unavailable",
            request=httpx.Request("POST", url),
            headers={"x-request-id": "req-scratch-failed"},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", failed_response)

    class Settlement:
        def settle_action_receipt(self, *, project, project_id, receipt_id):
            receipt = ReceiptStore(project).parsed_by_id(receipt_id)
            assert project_id == pid and receipt.status == "failed"
            assert receipt.provider_use
            settled.append(receipt_id)

    client.app.state.workspace.direct_action_receipt_settlement_port = Settlement()
    request = {"engine": "openai/whisper-1"}
    quote = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(request),
    ).json()
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart(
            {**request, "confirmation": quote["estimate"]["promise_set_hash"]}
        ),
    )
    assert started.status_code == 202, started.text
    result = _wait(client, pid, started.json()["preview_id"])
    assert result["status"] == "error"
    assert result["accounting"]["model_call_count"] == 1
    receipt_id = result["accounting"]["receipt_id"]
    receipt = ReceiptStore(project).parsed_by_id(receipt_id)
    assert receipt.status == "failed" and receipt.provider_use
    assert settled == [receipt_id]


def test_scratch_transport_failure_keeps_terminal_outcome_unresolved(
    tmp_path, monkeypatch
):
    """No returned provider fact cannot become a proven zero-cost failure."""
    client, pid = _client(tmp_path, monkeypatch)
    project = client.app.state.workspace.get(pid)
    entered = threading.Event()
    release = threading.Event()

    async def timeout(self, engine, path, spec, ctx, media):
        del self, engine, path, spec, ctx, media
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        raise HostedEngineError(
            code="transport",
            message="provider response timed out",
            retryable=True,
        )

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter, "transcribe", timeout
    )
    request = {"engine": "openai/whisper-1"}
    quote = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(request),
    ).json()
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart(
            {**request, "confirmation": quote["estimate"]["promise_set_hash"]}
        ),
    )
    assert entered.wait(5)
    [running_receipt] = ReceiptStore(project).index_rows()
    attempt = project.db.execute(
        "SELECT id FROM execution_attempts WHERE receipt_id=?",
        (running_receipt["id"],),
    ).fetchone()
    project.db.execute(
        "INSERT INTO attempt_row_authorizations "
        "(attempt_id,row_id,quoted_quantity) VALUES (?,?,?)",
        (attempt["id"], 1, "0.25"),
    )
    project.db.commit()
    release.set()
    result = _wait(client, pid, started.json()["preview_id"])
    assert result["status"] == "error"
    receipt_id = result["accounting"]["receipt_id"]
    [outcome] = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=(SELECT id FROM execution_attempts WHERE receipt_id=?)",
        (receipt_id,),
    ).fetchone()
    assert outcome is None


def test_cancellation_does_not_hide_an_ambiguous_transport_failure(
    tmp_path, monkeypatch
):
    """A concurrent cancel cannot turn an unknown provider effect into zero."""
    client, pid = _client(tmp_path, monkeypatch)
    project = client.app.state.workspace.get(pid)
    entered = threading.Event()
    release = threading.Event()

    async def timeout(self, engine, path, spec, ctx, media):
        del self, engine, path, spec, ctx, media
        entered.set()
        await asyncio.to_thread(release.wait, 5)
        raise HostedEngineError(
            code="transport",
            message="provider response timed out",
            retryable=True,
        )

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter, "transcribe", timeout
    )
    request = {"engine": "openai/whisper-1"}
    quote = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(request),
    ).json()
    started = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart(
            {**request, "confirmation": quote["estimate"]["promise_set_hash"]}
        ),
    )
    preview_id = started.json()["preview_id"]
    assert entered.wait(5)
    [running_receipt] = ReceiptStore(project).index_rows()
    attempt = project.db.execute(
        "SELECT id FROM execution_attempts WHERE receipt_id=?",
        (running_receipt["id"],),
    ).fetchone()
    project.db.execute(
        "INSERT INTO attempt_row_authorizations "
        "(attempt_id,row_id,quoted_quantity) VALUES (?,?,?)",
        (attempt["id"], 1, "0.25"),
    )
    project.db.commit()
    assert (
        client.delete(
            f"/api/projects/{pid}/actions/v1/preview/{preview_id}"
        ).status_code
        == 204
    )
    release.set()
    result = _wait(client, pid, preview_id)
    assert result["status"] == "cancelled"
    receipt_id = result["accounting"]["receipt_id"]
    [outcome] = project.db.execute(
        "SELECT terminal_outcome FROM attempt_row_authorizations "
        "WHERE attempt_id=(SELECT id FROM execution_attempts WHERE receipt_id=?)",
        (receipt_id,),
    ).fetchone()
    assert outcome is None


def test_scratch_confirmation_is_bound_to_bytes_and_interval(tmp_path, monkeypatch):
    client, pid = _client(tmp_path, monkeypatch, duration=900.0)
    first = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart({"engine": "openai/whisper-1", "time_limit_seconds": 60}),
    ).json()["estimate"]["promise_set_hash"]
    changed_interval = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart(
            {
                "engine": "openai/whisper-1",
                "time_limit_seconds": 61,
                "confirmation": first,
            }
        ),
    )
    assert changed_interval.status_code == 402

    changed_bytes = {
        "files": {"file": ("sample.wav", WAV + b"changed", "audio/wav")},
        "data": {
            "payload": json.dumps(
                {
                    "engine": "openai/whisper-1",
                    "time_limit_seconds": 60,
                    "confirmation": first,
                }
            )
        },
    }
    assert (
        client.post(
            f"/api/projects/{pid}/transcribe/compare-scratch", **changed_bytes
        ).status_code
        == 402
    )
