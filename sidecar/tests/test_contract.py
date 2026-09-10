"""Contract tests: the HTTP surface the app already speaks, engines stubbed.

The clients are the contract — src/frisket/ops/ocr.py `_ocr_sidecar` and
src/frisket/ops/convert.py `_convert_sidecar` in the main repo POST exactly
the shapes replicated here (multipart field name ``files``, form field
``engine``, ``Authorization: Bearer``, retry-on-429), and llm/adapters.py's
``embed`` defines the /v1/embeddings dialect. Change a shape here only if you
change those clients in the same breath."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket_models.app import create_app
from frisket_models.transcription.contract import CONTRACT_VERSION
from stub_helpers import AUTH, TOKEN, stub_registry

PNG = b"\x89PNG\r\n\x1a\n stub-bytes"


# ---------- startup: never anonymous ----------


class TestStartup:
    def test_no_token_refuses_to_start(self, monkeypatch):
        monkeypatch.delenv("FRISKET_MODELS_TOKEN", raising=False)
        with pytest.raises(RuntimeError, match="FRISKET_MODELS_TOKEN"):
            create_app(registry=stub_registry())

    def test_token_from_env(self, monkeypatch):
        monkeypatch.setenv("FRISKET_MODELS_TOKEN", "env-secret")
        app = create_app(registry=stub_registry())
        c = TestClient(app)
        assert c.get("/capabilities").status_code == 401
        ok = c.get("/capabilities", headers={"Authorization": "Bearer env-secret"})
        assert ok.status_code == 200

    def test_concurrency_from_env(self, monkeypatch):
        monkeypatch.setenv("FRISKET_MODELS_CONCURRENCY", "7")
        app = create_app(token=TOKEN, registry=stub_registry())
        assert app.state.limiter.limit == 7


# ---------- auth: shared bearer token on everything but /health ----------


class TestAuth:
    def test_health_is_open_and_says_nothing(self, client):
        assert client.get("/health").json() == {"ok": True}

    @pytest.mark.parametrize(
        "method,route",
        [
            ("GET", "/capabilities"),
            ("POST", "/ocr"),
            ("POST", "/to-markdown"),
            ("POST", "/v1/transcribe"),
            ("POST", "/ner"),
            ("POST", "/rerank"),
            ("POST", "/v1/embeddings"),
        ],
    )
    def test_missing_token_is_401(self, client, method, route):
        assert client.request(method, route).status_code == 401

    def test_wrong_token_is_403(self, client):
        resp = client.get("/capabilities", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_bearer_header_matches_ops_clients(self, client):
        # ops/ocr.py + ops/convert.py send exactly
        # {"Authorization": f"Bearer {token}"}
        resp = client.get("/capabilities", headers=AUTH)
        assert resp.status_code == 200


# ---------- capabilities: what's actually loadable, truthfully ----------


class TestCapabilities:
    def test_shape(self, client):
        body = client.get("/capabilities", headers=AUTH).json()
        assert body["service"] == "frisket-models"
        assert {"limit", "in_flight"} <= body["concurrency"].keys()
        by_name = {e["name"]: e for e in body["engines"]}
        # the settled engine order: docling+dots.mocr first, gliner second
        for name, route in [
            ("dots.mocr", "/ocr"),
            ("docling", "/to-markdown"),
            ("gliner", "/ner"),
            ("whisper-turbo", "/v1/transcribe"),
            ("cross-encoder", "/rerank"),
            ("fastembed", "/v1/embeddings"),
        ]:
            entry = by_name[name]
            assert entry["route"] == route
            assert {"available", "loaded", "models", "error"} <= entry.keys()
        turbo = by_name["whisper-turbo"]
        assert turbo["contract_versions"] == [CONTRACT_VERSION]
        assert turbo["options"] == {
            "diarization_mode": "none",
            "speaker_hint": "none",
            "language": True,
            "model_size": False,
            "vad": True,
            "context": True,
            "clean": False,
        }

    def test_missing_extra_reads_unavailable(self, client):
        body = client.get("/capabilities", headers=AUTH).json()
        entry = next(e for e in body["engines"] if e["name"] == "docling-missing")
        assert entry["available"] is False

    def test_loader_failure_surfaces_as_error(self, client):
        # first use trips the broken loader → 503; capabilities then reports
        # the recorded error instead of pretending the engine works
        resp = client.post(
            "/ocr",
            files=[("files", ("p.png", PNG, "image/png"))],
            data={"engine": "dots.mocr-broken"},
            headers=AUTH,
        )
        assert resp.status_code == 503
        assert "failed to load" in resp.json()["detail"]
        body = client.get("/capabilities", headers=AUTH).json()
        entry = next(e for e in body["engines"] if e["name"] == "dots.mocr-broken")
        assert entry["available"] is False
        assert "torch exploded" in entry["error"]


# ---------- backpressure: semaphore + 429, no queue ----------


class TestBackpressure:
    def test_429_when_full_with_retry_after(self, client):
        limiter = client.app.state.limiter
        taken = 0
        while limiter.acquire():  # fill every slot
            taken += 1
        try:
            resp = client.post(
                "/ner",
                json={"texts": ["x"], "labels": ["person"]},
                headers=AUTH,
            )
            assert resp.status_code == 429
            assert resp.headers.get("Retry-After")
        finally:
            for _ in range(taken):
                limiter.release()
        # slots free again → the ops clients' sleep-and-retry loop succeeds
        resp = client.post(
            "/ner", json={"texts": ["x"], "labels": ["person"]}, headers=AUTH
        )
        assert resp.status_code == 200

    def test_health_and_capabilities_ignore_backpressure(self, client):
        limiter = client.app.state.limiter
        taken = 0
        while limiter.acquire():
            taken += 1
        try:
            assert client.get("/health").status_code == 200
            assert client.get("/capabilities", headers=AUTH).status_code == 200
        finally:
            for _ in range(taken):
                limiter.release()


# ---------- /ocr: exactly what ops/ocr.py._ocr_sidecar sends/reads ----------


class TestOcr:
    def test_matches_ops_ocr_client(self, client):
        # the client: files=[("files", (p.name, p.read_bytes(), "image/png"))],
        # data={"engine": engine}, then reads resp.json()["pages"]
        files = [
            ("files", ("page-1.png", PNG, "image/png")),
            ("files", ("page-2.png", PNG, "image/png")),
        ]
        resp = client.post(
            "/ocr", files=files, data={"engine": "dots.mocr"}, headers=AUTH
        )
        assert resp.status_code == 200
        pages = resp.json()["pages"]
        assert len(pages) == 2  # array batching: one entry per part, in order
        for page in pages:
            assert isinstance(page["text"], str)
            for block in page["blocks"]:
                assert isinstance(block["text"], str)
                # bbox = 4-corner polygon, same shape as the rapidocr worker
                assert len(block["bbox"]) == 4
                assert all(len(pt) == 2 for pt in block["bbox"])
                assert 0.0 <= block["score"] <= 1.0

    def test_unknown_engine_is_400(self, client):
        resp = client.post(
            "/ocr",
            files=[("files", ("p.png", PNG, "image/png"))],
            data={"engine": "tesseract"},
            headers=AUTH,
        )
        assert resp.status_code == 400
        assert "tesseract" in resp.json()["detail"]

    def test_engine_for_other_route_is_400(self, client):
        resp = client.post(
            "/ocr",
            files=[("files", ("p.png", PNG, "image/png"))],
            data={"engine": "docling"},
            headers=AUTH,
        )
        assert resp.status_code == 400

    def test_missing_extra_is_503_with_install_hint(self, client):
        resp = client.post(
            "/to-markdown",
            files=[("files", ("d.pdf", b"%PDF- stub", "application/octet-stream"))],
            data={"engine": "docling-missing"},
            headers=AUTH,
        )
        assert resp.status_code == 503
        assert "not installed" in resp.json()["detail"]


# ---------- /to-markdown: ops/convert.py._convert_sidecar's contract ----------


class TestToMarkdown:
    def test_matches_ops_convert_client(self, client):
        # the client: files=[("files", (path.name, bytes,
        # "application/octet-stream"))], data={"engine": engine}; reads
        # body["documents"] (or "results"), docs[0]["markdown"] +
        # docs[0].get("ocr_used") or []
        resp = client.post(
            "/to-markdown",
            files=[
                ("files", ("report.pdf", b"%PDF- stub", "application/octet-stream"))
            ],
            data={"engine": "docling"},
            headers=AUTH,
        )
        assert resp.status_code == 200
        body = resp.json()
        docs = body.get("documents") or body.get("results")
        doc = docs[0] if docs else body  # the client's exact unwrap
        assert isinstance(doc.get("markdown", ""), str) and doc["markdown"]
        assert isinstance(doc.get("ocr_used") or [], list)

    def test_array_batching(self, client):
        files = [
            ("files", ("a.pdf", b"%PDF- a", "application/octet-stream")),
            ("files", ("b.pdf", b"%PDF- bb", "application/octet-stream")),
        ]
        resp = client.post(
            "/to-markdown", files=files, data={"engine": "docling"}, headers=AUTH
        )
        docs = resp.json()["documents"]
        assert len(docs) == 2
        assert "a.pdf" in docs[0]["markdown"]  # aligned to the parts
        assert "b.pdf" in docs[1]["markdown"]


# ---------- transcription v1: controls + strict result ----------


class TestTranscribe:
    def test_segments_and_timestamps(self, client):
        resp = client.post(
            "/v1/transcribe",
            files=[("files", ("clip.wav", b"RIFF stub", "audio/wav"))],
            data={
                "contract_version": CONTRACT_VERSION,
                "engine": "whisper-turbo",
                "options": '{"language":"fr","vad":false,"context":"Frisket"}',
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["text"]
        seg = result["segments"][0]
        assert {"start", "end", "text"} <= seg.keys()
        assert seg["start"] <= seg["end"]
        assert result["language"] == "fr"
        assert result["accepted_options"] == {
            "language": "fr",
            "vad": False,
            "context": "Frisket",
        }

    def test_unsupported_model_size_refuses_before_model_load(self, client):
        engine = client.app.state.registry.get("whisper-turbo")
        assert engine.loaded is False
        resp = client.post(
            "/v1/transcribe",
            files=[("files", ("clip.wav", b"RIFF stub", "audio/wav"))],
            data={
                "contract_version": CONTRACT_VERSION,
                "engine": "whisper-turbo",
                "options": '{"model_size":"small"}',
            },
            headers=AUTH,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "unsupported_option"
        assert engine.loaded is False


# ---------- /ner: GLiNER entities ----------


class TestNer:
    def test_entities_per_text(self, client):
        resp = client.post(
            "/ner",
            json={
                "texts": ["The mayor met Ada.", "Nothing here."],
                "labels": ["person"],
                "threshold": 0.4,
            },
            headers=AUTH,
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        assert len(results) == 2  # aligned to texts
        ent = results[0][0]
        assert {"text", "label", "start", "end", "score"} <= ent.keys()
        assert ent["label"] == "person"

    def test_empty_labels_is_400(self, client):
        resp = client.post("/ner", json={"texts": ["x"], "labels": []}, headers=AUTH)
        assert resp.status_code == 400


# ---------- /rerank: scored order ----------


class TestRerank:
    def test_ranked_best_first_with_index_into_request(self, client):
        docs = ["short", "a much longer and more relevant document", "mid doc"]
        resp = client.post(
            "/rerank",
            json={"query": "relevant?", "documents": docs},
            headers=AUTH,
        )
        results = resp.json()["results"]
        assert [r["index"] for r in results] == [1, 2, 0]  # stub: longer wins
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_top_k(self, client):
        resp = client.post(
            "/rerank",
            json={"query": "q", "documents": ["a", "bb", "ccc"], "top_k": 2},
            headers=AUTH,
        )
        assert len(resp.json()["results"]) == 2

    def test_empty_documents(self, client):
        resp = client.post(
            "/rerank", json={"query": "q", "documents": []}, headers=AUTH
        )
        assert resp.json()["results"] == []


# ---------- /v1/embeddings: the ONLY OpenAI-shaped route ----------


class TestEmbeddings:
    def test_matches_router_embed_dialect(self, client):
        # llm/adapters.py embed(): POST {model, input}, then
        # sorted(out["data"], key=index) → r["embedding"] per input, in order
        resp = client.post(
            "/v1/embeddings",
            json={"model": "anything", "input": ["one", "two"]},
            headers=AUTH,
        )
        assert resp.status_code == 200
        out = resp.json()
        rows = sorted(out["data"], key=lambda d: d["index"])
        vectors = [r["embedding"] for r in rows]
        assert len(vectors) == 2
        assert all(isinstance(v, list) and v for v in vectors)
        assert out["object"] == "list"
        assert out["model"] == "stub-embed"  # echoes what's loaded, not asked

    def test_single_string_input(self, client):
        resp = client.post("/v1/embeddings", json={"input": "just one"}, headers=AUTH)
        assert len(resp.json()["data"]) == 1
