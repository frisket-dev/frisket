"""Scratch response facts must outlive parsing and scope-cleanup failures."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, closing
from types import SimpleNamespace

import pytest

from frisket.cloud_runtime_binding import CloudRuntimeBinding
from frisket.engine.store import Project
from frisket.server.services.scratch_action_preview import (
    ScratchActionPreviewRunContext,
)
from tests.test_cloud_llm_checkpoint import (
    _coordinator,
    _request,
    _response,
    _Transport,
)
from tests.server.test_paid_transcribe_compare_preview import _client, _multipart, _wait
from frisket.sdk.ops import transcribe_engines


@pytest.mark.parametrize("failure", [None, "body", "record"])
def test_cloud_scratch_records_returned_children_before_retirement(
    tmp_path, monkeypatch, failure
):
    with closing(Project.create(tmp_path / "scratch.frisket")) as project:
        coordinator = _coordinator(
            project, _Transport([_response(execution_id="returned")])
        )
        route = object()
        binding = CloudRuntimeBinding(None, coordinator, route)
        context = ScratchActionPreviewRunContext(
            project=project,
            resolved_execution=None,
            receipt_id=None,
            attempt=SimpleNamespace(attempt_id="scope-fixture"),
            action_kind="media.ocr",
            router=None,
            composition=None,
            credential_use_context=None,
            execution_limits=None,
            progress=lambda *_: None,
            cancel_event=threading.Event(),
        )
        recorded = []

        def bind(actual_route, call):
            assert actual_route is route
            return dict(call)

        def record(self, calls):
            assert coordinator.returned_checkpoints, "must record before retiring"
            if failure == "record":
                raise RuntimeError("recording unavailable")
            from frisket.engine.store.runs import RunResultStore

            RunResultStore(project).write_unscoped_model_calls(
                list(calls), row_id=None, column_id=None
            )
            recorded.extend(calls)

        monkeypatch.setattr(
            ScratchActionPreviewRunContext, "_cloud_binding", lambda _: binding
        )
        monkeypatch.setattr(ScratchActionPreviewRunContext, "write_model_calls", record)
        monkeypatch.setattr(
            "frisket.execution.runtime_binding.bind_fact_to_route", bind
        )

        async def run():
            with context.action_context_scope():
                await coordinator.execute(_request())
                if failure == "body":
                    raise ValueError("OCR parser failed after provider returned")

        if failure is None:
            asyncio.run(run())
        else:
            with pytest.raises(ValueError if failure == "body" else RuntimeError):
                asyncio.run(run())
        if failure == "record":
            assert not recorded
            assert len(coordinator.returned_checkpoints) == 1
            assert (
                project.db.execute("SELECT state FROM effect_checkpoints").fetchone()[0]
                == "returned"
            )
        else:
            assert len(recorded) == 1
            assert recorded[0]["provider_cost_usd"] == pytest.approx(0.00001)
            assert not coordinator.returned_checkpoints
            assert (
                project.db.execute(
                    "SELECT COUNT(*) FROM effect_checkpoints"
                ).fetchone()[0]
                == 0
            )


def test_transcription_returned_cost_survives_scope_teardown_failure(
    tmp_path, monkeypatch
):
    client, pid = _client(tmp_path, monkeypatch)

    async def transcribe(self, engine, path, spec, ctx, media):
        return {
            "text": "returned",
            "segments": [],
            "language": "en",
            "duration": 15.0,
            "cost": 0.01,
            "cost_source": "pricing_data",
            "credential_source": ctx.extras["router"].credential_source_for("openai"),
        }

    original_scope = transcribe_engines.transcription_execution_scope

    @asynccontextmanager
    async def bad_cleanup(*args, **kwargs):
        async with original_scope(*args, **kwargs):
            yield
        raise RuntimeError("cleanup failed after response")

    monkeypatch.setattr(
        transcribe_engines.OpenAITranscriptionAdapter, "transcribe", transcribe
    )
    monkeypatch.setattr(
        transcribe_engines, "transcription_execution_scope", bad_cleanup
    )
    request = {"engine": "openai/whisper-1"}
    estimate = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch/estimate",
        **_multipart(request),
    )
    assert estimate.status_code == 200, estimate.text
    token = estimate.json()["estimate"]["promise_set_hash"]
    response = client.post(
        f"/api/projects/{pid}/transcribe/compare-scratch",
        **_multipart({**request, "confirmation": token}),
    )
    assert response.status_code == 202, response.text
    result = _wait(client, pid, response.json()["preview_id"])
    assert result["status"] == "error", result
    assert result["accounting"]["model_call_count"] == 1
    assert result["accounting"]["cost_actual"] == pytest.approx(0.01)
