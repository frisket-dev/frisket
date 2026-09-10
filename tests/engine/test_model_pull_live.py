"""Live Ollama pull proof, mirroring
``test_ollama_live.py``'s gate exactly: opt-in, $0, against localhost.

Gate semantics: without FRISKET_OLLAMA_LIVE=1 every test SKIPS (and the
`network` marker keeps it out of the default suite). The pinned model
(``smollm:135m``) is deliberately tiny and, per the design doc, likely
already installed on a dev machine that has run the other live Ollama
suite -- so this mostly exercises the idempotent fast path (GET /api/tags
finds it, no real download) end-to-end against a real daemon, while still
proving the handler's real HTTP flow (not a mock) works.

    ollama pull smollm:135m
    FRISKET_OLLAMA_LIVE=1 uv run pytest -m network -q tests/test_model_pull_live.py
"""

from __future__ import annotations

import os

import httpx
import pytest

from frisket.engine.jobs import model_pull_store as store
from frisket.engine.jobs.ports import JobHandlerContext
from frisket.engine.jobs.model_pull import (
    normalize_model_ref,
    register_model_pull_handler,
)
from frisket.engine.jobs.queue import open_queue
from frisket.engine.jobs.worker import HandlerRegistry

LIVE = os.environ.get("FRISKET_OLLAMA_LIVE") == "1"
pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(
        not LIVE, reason="live Ollama pull proof: set FRISKET_OLLAMA_LIVE=1 to run"
    ),
]

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
PULL_MODEL = os.environ.get("FRISKET_OLLAMA_PULL_MODEL", "smollm:135m")


def require_daemon() -> None:
    try:
        httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5).raise_for_status()
    except Exception as e:  # noqa: BLE001 — any transport failure gets remediation
        pytest.fail(
            f"FRISKET_OLLAMA_LIVE=1 but no Ollama daemon at {OLLAMA_URL} "
            f"({type(e).__name__}: {e}). Start it with `ollama serve`."
        )


def test_real_pull_of_a_tiny_model_converges(tmp_path, monkeypatch) -> None:
    require_daemon()
    monkeypatch.setenv("OLLAMA_URL", OLLAMA_URL)

    canonical = normalize_model_ref(PULL_MODEL)
    root = tmp_path / "ws"
    root.mkdir()
    queue = open_queue(workspace=root)
    try:
        registry = HandlerRegistry()
        register_model_pull_handler(registry, workspace_root=root, queue=queue)

        row, created = store.create_or_get_active(
            queue.engine, workspace_root=str(root), model_ref=canonical
        )
        assert created is True
        job_id = queue.enqueue(
            "model.pull", {"pull_id": row.id, "workspace_root": str(root)}
        )
        store.set_job_id(queue.engine, row.id, job_id=job_id)
        queue.claim("live-test-worker")

        handler = registry.get("model.pull")
        result = handler(
            {"pull_id": row.id, "workspace_root": str(root), "job_id": job_id},
            JobHandlerContext.without_job_row(),
        )

        assert result["status"] == "done"
        final = store.get(queue.engine, row.id)
        assert final.status == store.STATUS_DONE
        assert final.resolved_digest is not None
    finally:
        queue.close()
