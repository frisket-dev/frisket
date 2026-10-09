from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from frisket.server.routes.pdf_packet_splits import register_pdf_packet_split_routes


def _progress(status: str = "running", done: int = 0, total: int = 4) -> dict:
    return {"status": status, "done": done, "total": total, "error": None}


def _status() -> dict[str, Any]:
    return {
        "schema_version": "frisket.pdf_packet_split.v1",
        "split_id": "split-1",
        "status": "preparing",
        "packet": {
            "blob_hash": "a" * 64,
            "filename": "packet.pdf",
            "mime": "application/pdf",
            "size": 17,
            "page_count": 4,
        },
        "prepare": {
            "job_id": "prepare-1",
            "progress": _progress(),
            "pages_ready": [1],
            "native_text_pages": [1, 2],
            "visual_pages_ready": 1,
        },
        "text_source": "unconfirmed",
        "ocr_engine": None,
        "ocr_pages": [],
        "jobs": [],
        "analysis_revision": 1,
        "expires_at": "2026-10-09T01:00:00Z",
        "commit_result": None,
    }


class _Service:
    def __init__(self, tmp_path: Path) -> None:
        self.calls: list[tuple] = []
        self.thumb = tmp_path / "page-1.png"
        self.thumb.write_bytes(b"png")

    def create(self, pid, upload, *, request_id=None):
        self.calls.append(("create", pid, upload.filename, upload.sha256, request_id))
        return _status()

    def status(self, pid, split_id):
        return _status()

    def page(self, pid, split_id, page):
        return {
            "schema_version": "frisket.pdf_packet_split.v1",
            "split_id": split_id,
            "page": page,
            "thumbnail_ready": True,
            "thumbnail_url": f"/thumb/{page}",
            "native_text": "native",
            "ocr_text": None,
            "ocr_blocks": [],
            "ocr_engine": None,
        }

    def thumbnail(self, pid, split_id, page):
        return SimpleNamespace(path=self.thumb, close=lambda: None)

    def estimate_ocr(self, pid, split_id, body, *, request_context=None):
        self.calls.append(("estimate", body.engine, body.scope, body.pages))
        return {
            "schema_version": "frisket.pdf_packet_split.v1",
            "engine": body.engine,
            "scope": body.scope,
            "pages": body.pages or [1, 2, 3, 4],
            "cached_pages": [],
            "estimate": {
                "rows": 1,
                "cost": 0.0,
                "cost_source": "free_public_api",
                "billed_cost": 0,
                "policy_id": "frisket.identity.v1",
                "promise_set_hash": "free",
            },
        }

    def start_ocr(self, pid, split_id, body, *, request_context=None):
        return {
            "schema_version": "frisket.pdf_packet_split.v1",
            "split_id": split_id,
            "job_id": "ocr-1",
            "kind": "ocr_sample",
            "total": len(body.pages or []),
            "receipt_id": None,
        }

    def cancel_job(self, pid, split_id, job_id):
        return _status()

    def select_text_source(self, pid, split_id, body):
        value = _status()
        value["text_source"] = body.kind
        value["ocr_engine"] = body.engine
        value["ocr_pages"] = [1, 3] if body.kind == "ocr" else []
        return value

    def candidates(self, pid, split_id, body):
        self.calls.append(("candidates", body.confirmed_starts, body.rejected))
        return {
            "schema_version": "frisket.pdf_packet_split.v1",
            "split_id": split_id,
            "analysis_revision": 2,
            "clusters": [{"id": 0, "confirmed_pages": [1]}],
            "pages": [
                {
                    "page": 3,
                    "visual_score": 92.5,
                    "closest_confirmed_page": 1,
                    "kind_id": 0,
                    "matched_phrase_ids": ["letter"],
                    "suggested": True,
                }
            ],
            "suggested_pages": [3],
            "question_pages": [2],
            "phrase_counts": {"letter": 1},
            "accept_all_scope": "packet",
        }

    def commit(self, pid, split_id, body, *, request_context=None):
        self.calls.append(("commit", body.confirmed_starts, body.destination.name))
        return {
            "schema_version": "frisket.pdf_packet_split.v1",
            "split_id": split_id,
            "job_id": "commit-1",
            "kind": "commit",
            "total": len(body.confirmed_starts),
            "receipt_id": "receipt-1",
        }

    def close(self, pid, split_id):
        self.calls.append(("close", pid, split_id))


def _client(tmp_path: Path) -> tuple[TestClient, _Service]:
    app = FastAPI()
    service = _Service(tmp_path)
    register_pdf_packet_split_routes(app, service=service)  # type: ignore[arg-type]
    return TestClient(app), service


def test_pdf_packet_split_http_contract(tmp_path: Path) -> None:
    client, service = _client(tmp_path)
    created = client.post(
        "/api/projects/p/import/pdf-packet-splits",
        files={"file": ("packet.pdf", b"%PDF-test-packet", "application/pdf")},
        data={"request_id": "upload-1"},
    )
    assert created.status_code == 202, created.text
    assert created.json() == _status()
    assert service.calls[0][:3] == ("create", "p", "packet.pdf")
    assert service.calls[0][4] == "upload-1"

    page = client.get("/api/projects/p/import/pdf-packet-splits/split-1/pages/1")
    assert page.status_code == 200
    assert page.json()["native_text"] == "native"
    assert "quality" not in page.json()

    estimate = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/ocr/estimate",
        json={"engine": "rapidocr", "scope": "sample", "pages": [1, 3]},
    )
    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["pages"] == [1, 3]

    started = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/ocr/jobs",
        json={"engine": "rapidocr", "scope": "sample", "pages": [1, 3]},
    )
    assert started.status_code == 202
    assert started.json()["kind"] == "ocr_sample"

    selected = client.put(
        "/api/projects/p/import/pdf-packet-splits/split-1/text-source",
        json={"kind": "ocr", "engine": "rapidocr"},
    )
    assert selected.status_code == 200
    assert selected.json()["text_source"] == "ocr"
    assert selected.json()["ocr_pages"] == [1, 3]

    candidates = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/candidates",
        json={
            "confirmed_starts": [1],
            "rejected": [4],
            "threshold_pct": 80,
            "phrases": [
                {"id": "letter", "text": "Dear", "enabled": True, "fuzzy": False}
            ],
        },
    )
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["accept_all_scope"] == "packet"

    committed = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/commit",
        json={
            "idempotency_key": "commit-1",
            "confirmed_starts": [1, 3],
            "destination": {"kind": "new_sheet", "name": "Packet documents"},
            "name_pattern": "{packet} · pp {start}–{end}",
            "keep_ocr_text": True,
        },
    )
    assert committed.status_code == 202, committed.text
    assert committed.json()["total"] == 2

    closed = client.delete("/api/projects/p/import/pdf-packet-splits/split-1")
    assert closed.status_code == 204


def test_pdf_packet_split_rejects_incoherent_ocr_and_text_source_bodies(
    tmp_path: Path,
) -> None:
    client, service = _client(tmp_path)
    bad_sample = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/ocr/estimate",
        json={"engine": "rapidocr", "scope": "sample"},
    )
    bad_all = client.post(
        "/api/projects/p/import/pdf-packet-splits/split-1/ocr/estimate",
        json={"engine": "rapidocr", "scope": "all", "pages": [1]},
    )
    bad_source = client.put(
        "/api/projects/p/import/pdf-packet-splits/split-1/text-source",
        json={"kind": "ocr"},
    )
    assert (bad_sample.status_code, bad_all.status_code, bad_source.status_code) == (
        422,
        422,
        422,
    )
    assert service.calls == []
