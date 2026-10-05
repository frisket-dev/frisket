"""Clef HTTP and local-loading contracts; never fetch or execute model weights."""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.classification import clef
from frisket_models.engines import Engine, Registry, default_registry

AUTH = {"Authorization": "Bearer test-clef-token"}
QUESTION = {
    "type": "choice",
    "instructions": "Which team handles this?",
    "criteria": {"billing": "Invoices", "technical": "Outages"},
}
BODY = {
    "engine": "clef-flash",
    "text": "Orders are blocked",
    "questions": {"team": QUESTION},
}
RESPONSE = {
    "model": "clef-flash",
    "answers": {
        "team": {
            "type": "choice",
            "choice": "technical",
            "confidence": 0.9,
            "probabilities": {"billing": 0.1, "technical": 0.9},
        },
    },
    "usage": {"input_tokens": 42, "output_tokens": 0},
}


def make_client(adapter=None, loader=None):
    calls = []

    def classify(text, questions):
        calls.append((text, questions))
        return RESPONSE

    engine = Engine(
        "clef-flash", "/classify", [], loader or (lambda: adapter or classify)
    )
    app = create_app(
        token="test-clef-token", registry=Registry([engine]), concurrency=1
    )
    return TestClient(app), engine, calls


def test_native_answers_and_usage_preserved():
    client, engine, calls = make_client()
    assert not engine.loaded
    result = client.post("/classify", json=BODY, headers=AUTH)
    assert result.status_code == 200
    assert result.json() == RESPONSE
    assert calls == [(BODY["text"], BODY["questions"])]
    assert engine.loaded
    assert client.app.state.limiter.in_flight == 0


@pytest.mark.parametrize(
    "headers,status", [({}, 401), ({"Authorization": "Bearer wrong"}, 403)]
)
def test_auth_precedes_loading(headers, status):
    client, engine, calls = make_client()
    assert client.post("/classify", json=BODY, headers=headers).status_code == status
    assert not engine.loaded
    assert not calls


def test_full_limiter_does_not_load_engine():
    client, engine, _ = make_client()
    limiter = client.app.state.limiter
    assert limiter.acquire()
    try:
        result = client.post("/classify", json=BODY, headers=AUTH)
        assert result.status_code == 429
        assert result.headers["Retry-After"] == "2"
        assert not engine.loaded
    finally:
        limiter.release()


@pytest.mark.parametrize(
    "patch",
    [
        {"text": ""},
        {"text": "x" * (clef.MAX_TEXT_CHARS + 1)},
        {"questions": {}},
        {"engine": "clef"},
        {"images": ["private-path"]},
        {"questions": {"x": {"type": "choice", "criteria": {"a": "one"}}}},
        {"questions": {"x": {"type": "score", "criteria": {"a": "one", "b": "two"}}}},
        {"questions": {"x": {"type": "noul", "criteria": {"maybe": "yes"}}}},
        {"questions": {str(n): {"type": "noul"} for n in range(65)}},
        {
            "questions": {
                str(n): {
                    "type": "choice",
                    "criteria": {str(i): "x" for i in range(254)},
                }
                for n in range(3)
            }
        },
    ],
)
def test_invalid_requests_do_not_load_or_echo_text(patch):
    client, engine, _ = make_client()
    response = client.post("/classify", json={**BODY, **patch}, headers=AUTH)
    assert response.status_code == 422
    assert BODY["text"] not in response.text
    assert not engine.loaded
    assert client.app.state.limiter.in_flight == 0


def test_request_bytes_bounded_before_json_parse():
    client, engine, _ = make_client()
    response = client.post(
        "/classify", content=b" " * (clef.MAX_REQUEST_BYTES + 1), headers=AUTH
    )
    assert response.status_code == 413
    assert not engine.loaded
    assert client.app.state.limiter.in_flight == 0


def test_noul_and_ordered_score_questions_reach_head():
    client, _, calls = make_client()
    questions = {
        "yes": {"type": "noul", "instructions": "Is this an outage?"},
        "urgency": {"type": "score", "criteria": ["Low", "Medium", "High"]},
    }
    result = client.post(
        "/classify", json={**BODY, "questions": questions}, headers=AUTH
    )
    assert result.status_code == 200
    assert calls[0][1]["yes"] == questions["yes"]
    assert calls[0][1]["urgency"]["criteria"] == ["Low", "Medium", "High"]


@pytest.mark.parametrize(
    "error,status", [(ValueError("private text"), 422), (RuntimeError("secret"), 500)]
)
def test_inference_failure_sanitized_and_slot_released(error, status):
    def broken(*args):
        raise error

    client, _, _ = make_client(adapter=broken)
    result = client.post("/classify", json=BODY, headers=AUTH)
    assert result.status_code == status
    assert str(error) not in result.text
    assert client.app.state.limiter.in_flight == 0


def test_capability_probe_stays_lazy_and_missing_extra_is_unavailable(monkeypatch):
    monkeypatch.delenv(clef.SNAPSHOT_ENV, raising=False)
    engine = default_registry().get("clef-flash")
    engine.modules = ["frisket_clef_deliberately_missing_module"]
    description = engine.describe()
    assert description["route"] == "/classify"
    assert description["available"] is False
    assert description["loaded"] is False
    assert description["models"] == [clef.MODEL_ID]
    with pytest.raises(RuntimeError, match="classify-clef"):
        engine.get()


def provision_stub_snapshot(tmp_path, monkeypatch):
    path = tmp_path / clef.MODEL_REVISION
    path.mkdir()
    for filename in (
        "config.json",
        "model.safetensors.index.json",
        "joint_head.safetensors",
        "joint_head_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "processor_config.json",
    ):
        (path / filename).touch()
    monkeypatch.setenv(clef.SNAPSHOT_ENV, str(path))
    return path


def test_lazy_adapter_calls_joint_systemone_head(tmp_path, monkeypatch):
    path = provision_stub_snapshot(tmp_path, monkeypatch)
    calls = []
    runtime = types.ModuleType("frisket_models.classification._clef_inference")

    def load_release_model(snapshot, **kwargs):
        calls.append((snapshot, kwargs))
        return "model", "processor"

    def systemone(model, processor, request, **kwargs):
        calls.append((model, processor, request, kwargs))
        return RESPONSE

    runtime.load_release_model = load_release_model
    runtime.systemone = systemone
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    adapter = clef.load_clef_flash()
    assert calls == [(path, {"device": "cuda"})]
    assert adapter(BODY["text"], BODY["questions"]) == RESPONSE
    assert calls[1] == (
        "model",
        "processor",
        {"model": "clef-flash", "state": BODY["text"], "questions": BODY["questions"]},
        {"max_length": 16384},
    )


def test_model_loading_failure_does_not_disclose_paths(tmp_path, monkeypatch):
    provision_stub_snapshot(tmp_path, monkeypatch)
    runtime = types.ModuleType("frisket_models.classification._clef_inference")

    def broken(*args, **kwargs):
        raise RuntimeError("private path and token")

    runtime.load_release_model = broken
    runtime.systemone = None
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    with pytest.raises(RuntimeError, match="could not load") as exc:
        clef.load_clef_flash()
    assert "private" not in str(exc.value)


def test_first_use_download_is_pinned_and_excludes_code(tmp_path, monkeypatch):
    path = provision_stub_snapshot(tmp_path, monkeypatch)
    monkeypatch.delenv(clef.SNAPSHOT_ENV)
    downloads = []
    hub = types.ModuleType("huggingface_hub")

    def download(model_id, **kwargs):
        downloads.append((model_id, kwargs))
        return str(path)

    hub.snapshot_download = download
    monkeypatch.setitem(sys.modules, hub.__name__, hub)
    runtime = types.ModuleType("frisket_models.classification._clef_inference")
    runtime.load_release_model = lambda *args, **kwargs: ("model", "processor")
    runtime.systemone = lambda *args, **kwargs: RESPONSE
    monkeypatch.setitem(sys.modules, runtime.__name__, runtime)
    engine = Engine("clef-flash", "/classify", [], clef.load_clef_flash)
    assert engine.describe()["loaded"] is False
    assert not downloads
    engine.get()
    engine.get()
    assert downloads == [
        (
            clef.MODEL_ID,
            {
                "revision": clef.MODEL_REVISION,
                "allow_patterns": ["*.json", "*.safetensors", "chat_template.jinja"],
                "token": False,
            },
        )
    ]


def test_download_error_is_sanitized_and_capability_becomes_unavailable(monkeypatch):
    monkeypatch.delenv(clef.SNAPSHOT_ENV, raising=False)
    hub = types.ModuleType("huggingface_hub")

    def broken(*args, **kwargs):
        raise RuntimeError("private token and cache path")

    hub.snapshot_download = broken
    monkeypatch.setitem(sys.modules, hub.__name__, hub)
    engine = Engine("clef-flash", "/classify", [], clef.load_clef_flash)
    with pytest.raises(RuntimeError, match="could not download"):
        engine.get()
    description = engine.describe()
    assert description["available"] is False
    assert description["loaded"] is False
    assert "private" not in description["error"]


def test_vendored_encoding_rejects_truncation_and_native_answers():
    pytest.importorskip("torch")
    from frisket_models.classification._clef_inference import (
        encode_record,
        systemone_answer,
    )

    def tokenizer(text, **kwargs):
        return types.SimpleNamespace(input_ids=list(range(len(text))))

    record = {"state": "x" * 2000, "questions": BODY["questions"]}
    with pytest.raises(ValueError, match="context limit"):
        encode_record(tokenizer, record, max_length=1000)
    assert systemone_answer({"type": "noul"}, {"true": 0.75, "false": 0.25}) == {
        "type": "noul",
        "noul": 0.75,
    }
    score = systemone_answer(
        {"type": "score", "criteria": ["Low", "Mid", "High"]},
        {"0": 0.1, "1": 0.2, "2": 0.7},
    )
    assert score["score"] == 1.6
    assert score["confidence"] == 0.7
