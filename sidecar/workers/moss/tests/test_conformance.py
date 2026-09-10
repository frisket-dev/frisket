"""End-to-end conformance: MossAdapter through the real worker wrapper.

The MOSS native server (Boundary D) is replaced by an httpx MockTransport so the
whole Boundary-C path — request shaping, error mapping, transcript parsing, and
contract validation inside ``create_worker_app`` — is proven on CPU.  The live
GPU diarized burn is a separate, shared gate.
"""

from __future__ import annotations

import io
from pathlib import Path

import httpx
import pytest
from frisket_models.transcription import (
    AdapterRegistration,
    EngineProbe,
)
from frisket_models.transcription.worker import create_worker_app
from starlette.testclient import TestClient

from frisket_worker_moss import app as app_module
from frisket_worker_moss.adapter import (
    DESCRIPTOR,
    MossAdapter,
    MossConfig,
    build_probe,
)

FIXTURES = Path(__file__).parent / "fixtures"
RUNTIME_IMAGE_ID = "oci:sha256:" + "c" * 64
CONTRACT_VERSION = "frisket.transcription.v1"


def test_app_forwards_shared_worker_upload_limit(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_create_worker_app(registration, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(app_module, "create_worker_app", fake_create_worker_app)
    monkeypatch.setenv("FRISKET_MOSS_WORKER_TOKEN", "worker-token")
    monkeypatch.setenv("FRISKET_MOSS_WORKER_CONCURRENCY", "1")
    monkeypatch.setenv("FRISKET_TRANSCRIPTION_WORKER_MAX_UPLOAD_BYTES", "1234")

    assert app_module.create_app() is sentinel
    assert captured == {
        "token": "worker-token",
        "concurrency": 1,
        "max_upload_bytes": 1234,
    }


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _build_client(handler) -> TestClient:
    """A worker app whose MOSS adapter talks to ``handler`` instead of a GPU."""

    captured: list[httpx.Request] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler(request)

    config = MossConfig()
    mock_client = httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(_capture)
    )
    registration = AdapterRegistration(
        descriptor=DESCRIPTOR,
        factory=lambda: MossAdapter(config, client=mock_client),
        probe=lambda: EngineProbe(available=True, loaded=True, error=None),
    )
    app = create_worker_app(registration, runtime_image_id=RUNTIME_IMAGE_ID)
    client = TestClient(app)
    client.captured = captured  # type: ignore[attr-defined]
    return client


def _post(client: TestClient, options_json: str, engine: str = "moss"):
    return client.post(
        "/v1/transcribe",
        data={
            "contract_version": CONTRACT_VERSION,
            "engine": engine,
            "options": options_json,
        },
        files={"file": ("clip.wav", io.BytesIO(b"RIFFfake-audio"), "audio/wav")},
    )


def _ok(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": text})

    return handler


def test_happy_path_returns_conformant_intrinsic_result():
    client = _build_client(_ok(_fixture("basic_two_speaker.txt")))
    response = _post(client, '{"context": "Kenobi, Skywalker"}')

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["engine"] == "moss"
    assert result["model_ids"] == [DESCRIPTOR.model_ids[0]]
    assert result["revision"] == DESCRIPTOR.revision
    # Intrinsic diarization: every segment has a speaker; no word timings.
    speakers = [seg["speaker"] for seg in result["segments"]]
    assert speakers == ["S01", "S02", "S01"]
    assert all(seg.get("words") is None for seg in result["segments"])
    # context is the only caller knob MOSS consumes; the worker enforces an exact
    # echo of the accepted options.
    assert result["accepted_options"] == {"context": "Kenobi, Skywalker"}
    # No language is asserted (MOSS auto-detects and does not report it back).
    assert result["language"] is None
    # device/dtype are image-committed constants, not env-forgeable fiction.
    assert result["device"] == "cuda"
    assert result["dtype"] == "bfloat16"
    assert result["duration"] == 18.76


def test_context_is_forwarded_and_language_is_never_sent():
    client = _build_client(_ok(_fixture("single_speaker.txt")))
    _post(client, '{"context": "Frisket"}')

    body = client.captured[0].content.decode("utf-8")  # type: ignore[attr-defined]
    # The hotword hint rides on the full default diarization prompt.
    assert 'name="prompt"' in body
    assert "热词提示：Frisket" in body
    # vLLM honours max_completion_tokens; the ignored max_new_tokens must be gone.
    assert 'name="max_completion_tokens"' in body
    assert "max_new_tokens" not in body
    # language is not a MOSS knob, so it is never forwarded.
    assert 'name="language"' not in body


def test_context_is_forwarded_verbatim_not_normalized():
    client = _build_client(_ok(_fixture("single_speaker.txt")))
    response = _post(client, '{"context": "  Frisket  "}')

    assert response.status_code == 200
    body = client.captured[0].content.decode("utf-8")  # type: ignore[attr-defined]
    assert "热词提示：  Frisket  " in body
    assert response.json()["result"]["accepted_options"] == {"context": "  Frisket  "}


def test_no_options_yields_empty_accepted_options():
    client = _build_client(_ok(_fixture("overlap.txt")))
    response = _post(client, "{}")

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["accepted_options"] == {}
    # Overlap preserved through the full pipeline.
    assert result["segments"][0]["end"] > result["segments"][1]["start"]


@pytest.mark.parametrize(
    "options_json",
    [
        '{"diarize": true}',
        '{"diarize": true, "num_speakers": 2}',
        '{"vad": true}',
    ],
)
def test_unsupported_knobs_are_rejected_before_the_server(options_json):
    client = _build_client(_ok(_fixture("single_speaker.txt")))
    response = _post(client, options_json)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_option"
    # The intrinsic engine never reached its native server for a rejected knob.
    assert client.captured == []  # type: ignore[attr-defined]


def test_language_option_is_rejected_as_unsupported():
    client = _build_client(_ok(_fixture("single_speaker.txt")))
    response = _post(client, '{"language": "en"}')

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_option"
    # MOSS does not consume language, so it never reaches the server.
    assert client.captured == []  # type: ignore[attr-defined]


def test_duration_is_max_end_under_overlap():
    client = _build_client(_ok(_fixture("duration_overlap.txt")))
    response = _post(client, "{}")

    assert response.status_code == 200
    # An earlier, longer turn (ends 10.0) outlasts the last-by-start segment
    # (ends 6.0); duration must be the max, not the final segment's end.
    assert response.json()["result"]["duration"] == 10.0


def test_oversize_400_is_reclassified_as_input_too_large():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Maximum file size exceeded (42 MiB).",
                    "type": "BadRequestError",
                    "param": "audio_filesize_mb",
                    "code": 400,
                }
            },
        )

    client = _build_client(handler)
    response = _post(client, "{}")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"


def test_token_limit_400_is_a_server_fault_not_bad_audio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "max_tokens must be at least 1, got 0.",
                    "type": "BadRequestError",
                    "param": "max_tokens",
                    "code": 400,
                }
            },
        )

    client = _build_client(handler)
    response = _post(client, "{}")

    # Our misconfiguration, not the caller's audio: 500, not a 413/400 input error.
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


@pytest.mark.parametrize(
    "message",
    [
        "Invalid or unsupported audio file.",
        "Audio input is too short to produce any tokens.",
    ],
)
def test_pinned_invalid_audio_400_maps_to_a_client_input_error(message):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": message,
                    "type": "BadRequestError",
                    "param": None,
                    "code": 400,
                }
            },
        )

    client = _build_client(handler)
    response = _post(client, "{}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_audio"


def test_duration_bound_400_is_reclassified_as_input_too_large():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "Audio exceeds maximum allowed duration of 5700 seconds. "
                        "Set VLLM_MAX_AUDIO_DECODE_DURATION_S to change the limit."
                    ),
                    "type": "BadRequestError",
                    "param": None,
                    "code": 400,
                }
            },
        )

    response = _post(_build_client(handler), "{}")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"


@pytest.mark.parametrize("param", ["input_text", "input_tokens"])
def test_context_bound_400_is_reclassified_as_input_too_large(param):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "This model's maximum context length is 131072 tokens."
                    ),
                    "type": "BadRequestError",
                    "param": param,
                    "code": 400,
                }
            },
        )

    response = _post(_build_client(handler), '{"context": "many hotwords"}')

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"


def test_unclassified_400_is_a_server_fault_not_blindly_bad_audio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": "Audio file request schema changed.",
                    "type": "BadRequestError",
                    "param": "body.file",
                    "code": 400,
                }
            },
        )

    response = _post(_build_client(handler), "{}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


def test_malformed_model_output_propagates_as_inference_failure():
    # The parser's own malformed-input matrix (truncation, out-of-order,
    # missing end, ambiguity) is covered exhaustively in test_transcript.py;
    # here we only prove one such fault propagates through the full worker
    # stack as a 500 rather than surfacing a partial 200.
    response = _post(_build_client(_ok("[0][S01]missing-end[2][S02]good[3]")), "{}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


def test_non_json_success_body_is_an_inference_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>gateway error</html>",
            headers={"content-type": "text/html"},
        )

    response = _post(_build_client(handler), "{}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


@pytest.mark.parametrize("payload", [{"segments": []}, {"text": 12}])
def test_success_body_without_a_text_string_is_an_inference_failure(payload):
    # A 200 whose body omits (or mistypes) `text` is server-contract drift, not
    # a valid empty transcript — it must not be read as silence.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    response = _post(_build_client(handler), "{}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


def test_server_415_maps_to_invalid_audio():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            415, json={"error": {"message": "unsupported media type"}}
        )

    response = _post(_build_client(handler), "{}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_audio"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"probe_timeout_seconds": float("inf")}, "probe_timeout_seconds"),
        ({"max_completion_tokens": 0}, "max_completion_tokens"),
    ],
)
def test_invalid_adapter_configuration_fails_at_construction(kwargs, message):
    with pytest.raises(ValueError, match=message):
        MossConfig(**kwargs)


def test_from_env_reads_overrides_but_keeps_base_url_locked():
    config = MossConfig.from_env(
        {
            "FRISKET_MOSS_TIMEOUT_SECONDS": "12.5",
            "FRISKET_MOSS_PROBE_TIMEOUT_SECONDS": "3",
            "FRISKET_MOSS_MAX_COMPLETION_TOKENS": "2048",
        }
    )

    assert config.timeout_seconds == 12.5
    assert config.probe_timeout_seconds == 3.0
    assert config.max_completion_tokens == 2048
    # base_url is not env-derived: it stays pinned to the co-located loopback.
    assert config.base_url == "http://127.0.0.1:8000"


def test_from_env_falls_back_to_the_pinned_defaults():
    assert MossConfig.from_env({}) == MossConfig()


def test_native_server_url_cannot_be_overridden():
    with pytest.raises(TypeError, match="base_url"):
        MossConfig(base_url="http://external.example")  # type: ignore[call-arg]


def test_default_native_client_ignores_proxy_environment():
    adapter = MossAdapter(MossConfig())
    try:
        assert adapter._client._trust_env is False  # noqa: SLF001
    finally:
        adapter._client.close()  # noqa: SLF001


def test_probe_ignores_proxy_environment(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(url=url, **kwargs)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "get", fake_get)

    assert build_probe(MossConfig())().available is True
    assert captured == {
        "url": "http://127.0.0.1:8000/health",
        "timeout": 2.0,
        "trust_env": False,
    }


def test_probe_reports_unavailable_on_unhealthy_status(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(503))

    probe = build_probe(MossConfig())()

    assert probe.available is False
    assert probe.loaded is False
    assert "503" in probe.error


def test_probe_reports_unavailable_when_server_is_unreachable(monkeypatch):
    def refuse(url, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", refuse)

    probe = build_probe(MossConfig())()

    assert probe.available is False
    assert probe.loaded is False
    assert "ConnectError" in probe.error


def test_server_413_maps_to_input_too_large():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": {"message": "audio too long"}})

    client = _build_client(handler)
    response = _post(client, "{}")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "input_too_large"


def test_server_500_maps_to_inference_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "cuda oom"}})

    client = _build_client(handler)
    response = _post(client, "{}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "inference_failed"


def test_capabilities_does_not_touch_the_server():
    client = _build_client(_ok(_fixture("single_speaker.txt")))
    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    payload = response.json()
    assert payload["descriptor"]["options"]["diarization_mode"] == "intrinsic"
    assert payload["descriptor"]["options"]["speaker_hint"] == "none"
    assert payload["descriptor"]["runtime_image_id"] == RUNTIME_IMAGE_ID
    assert client.captured == []  # type: ignore[attr-defined]
