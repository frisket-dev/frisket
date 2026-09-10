"""Action/receipt projection tests for invocation-scoped recipe halts."""

from __future__ import annotations

import frisket.sdk.ops.transcribe_engines as transcribe_engines

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from frisket.contracts.action import (
    ActionResult,
    Receipt,
)
from frisket.actions.system import root_action_catalog
from frisket.engine.executor import ExecutorDeps, run_action_spec
from tests.execution_composition_helpers import open_attempt_authority
from frisket.ai.llm import ModelRouter
from frisket.engine.store.media_blobs import media_cell
from frisket.ops.base import RecipeInvocationHalt
from frisket.engine.runner import MapRunner
from frisket.engine.sandbox.shim import SandboxTeardownError
from frisket.server.app import create_app
from frisket.engine.store import Project
from http_test_helpers import drain_queue


PROJECT_ID = "run-scoped-action-projection"


def _seed_media_project(path: Path) -> tuple[Project, int, list[int]]:
    project = Project.create(path, name="Run-scoped action projection")
    sheet_id = project.add_sheet("Episodes")
    columns = {
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    blobs = [
        project.add_blob(
            b"RIFF0000WAVEfmt " + label.encode("ascii"),
            filename=f"{label}.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for label in ("one", "two")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    blob,
                    mime="audio/wav",
                    filename=f"{index}.wav",
                )
            }
            for index, blob in enumerate(blobs)
        ],
        columns,
    )
    return project, sheet_id, row_ids


def _transcribe_action(
    sheet_id: int,
    row_ids: list[int],
    *,
    key: str,
    output_name: str = "transcript",
) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {"source": "media", "engine": "faster_whisper"},
        "output_names": {
            "text": output_name,
            "segments": output_name + "_segments",
            "detected_language": output_name + "_language",
        },
        "idempotency_key": key,
    }


def _backfill_action(sheet_id: int, column: str, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "run.backfill",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"column": column},
        "idempotency_key": key,
    }


def _receipt(project: Project, receipt_id: str) -> Receipt:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return Receipt.model_validate(json.loads(row["body"]))


def _transcript(text: str) -> dict[str, Any]:
    return {
        "text": text,
        "segments": [{"start": 0.0, "end": 0.5, "text": text}],
        "language": "en",
        "duration": 0.5,
    }


def test_action_catalogs_declare_invocation_halt_codes() -> None:
    entries = {
        entry.kind: entry.model_dump(mode="json")
        for entry in root_action_catalog().actions
    }
    expected = {
        "local_engine_busy",
        "local_artifact_unavailable",
        "local_session_failed",
    }
    for kind in ("media.transcribe", "run.backfill"):
        assert expected <= {error["code"] for error in entries[kind]["errors"]}


def test_direct_halt_and_operator_cancel_remain_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, row_ids = _seed_media_project(tmp_path / "direct.frisket")
    original_scope = transcribe_engines.transcription_execution_scope

    @asynccontextmanager
    async def halted_scope(
        spec: dict[str, Any],
        ctx: Any,
        *,
        expected_rows: int,
    ):
        del spec, ctx, expected_rows
        raise RecipeInvocationHalt(
            "local_engine_busy",
            "another local transcription is still finishing",
        )
        yield  # pragma: no cover - makes this an async context manager

    monkeypatch.setattr(
        transcribe_engines, "transcription_execution_scope", halted_scope
    )
    try:
        halted = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_ids,
                key="media-transcribe@sha256:typed-halt",
            ),
            project_id=PROJECT_ID,
        )

        assert halted.status == "failed"
        assert halted.errors[0].code == "local_engine_busy"
        assert halted.errors[0].details == {
            "run_id": halted.run_id,
            "completed_rows": 0,
            "total_rows": 2,
        }
        assert (
            halted.outputs == []
        )  # no rows were published before scope admission halted
        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (halted.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        assert json.loads(run["params"])["halted_code"] == "local_engine_busy"
        halted_receipt = _receipt(project, str(halted.receipt_id))
        assert halted_receipt.status == "failed"
        assert halted_receipt.errors[0].code == "local_engine_busy"

        monkeypatch.setattr(
            transcribe_engines, "transcription_execution_scope", original_scope
        )
        operator = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_ids,
                key="media-transcribe@sha256:operator-cancel",
                output_name="operator_transcript",
            ),
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                map_runner_factory=lambda target, router: MapRunner(
                    target,
                    router or ModelRouter(keys={}),
                    should_cancel=lambda _run_id: True,
                    # These transcribe actions resolve an execution route, and
                    # a routed run never dispatches unverified: a test factory
                    # must wire the SAME attempt authority the production
                    # factories do (engine/executor/actions.py), or the
                    # effect-site fence refuses before any row runs and the
                    # halt/cancel distinction under test never happens.
                    authority=open_attempt_authority(target),
                )
            ),
        )

        assert operator.status == "cancelled"
        assert operator.errors == []
        operator_run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (operator.run_id,)
        ).fetchone()
        assert operator_run["status"] == "cancelled"
        operator_params = json.loads(operator_run["params"])
        assert "halted_code" not in operator_params
        assert "halted_reason" not in operator_params
        operator_receipt = _receipt(project, str(operator.receipt_id))
        assert operator_receipt.status == "cancelled"
        assert operator_receipt.errors == []
    finally:
        project.close()


def test_direct_sandbox_teardown_persists_typed_partial_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, row_ids = _seed_media_project(tmp_path / "teardown.frisket")
    calls = 0

    async def teardown_second_row(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        nonlocal calls
        del self, path, spec
        calls += 1
        if calls == 2:
            raise SandboxTeardownError("owned process tree leaked secret diagnostic")
        return _transcript("committed transcript")

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", teardown_second_row
    )
    try:
        result = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_ids,
                key="media-transcribe@sha256:direct-sandbox-teardown",
            ),
            project_id=PROJECT_ID,
            deps=ExecutorDeps(
                map_runner_factory=lambda target, router: MapRunner(
                    target,
                    router or ModelRouter(keys={}),
                    concurrency=1,
                    # See above: routed runs verify at dispatch, test
                    # factories included.
                    authority=open_attempt_authority(target),
                )
            ),
        )

        assert calls == 2
        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["local_session_failed"]
        assert result.errors[0].details == {
            "run_id": result.run_id,
            "completed_rows": 1,
            "total_rows": 2,
        }
        assert result.outputs
        assert all(output.row_ids == row_ids[:1] for output in result.outputs)

        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (result.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        params = json.loads(run["params"])
        assert params["halted_code"] == "local_session_failed"
        assert "secret diagnostic" not in params["halted_reason"]
        assert (
            project.db.execute(
                "SELECT COUNT(DISTINCT row_id) FROM results WHERE run_id=?",
                (result.run_id,),
            ).fetchone()[0]
            == 1
        )

        receipt = _receipt(project, str(result.receipt_id))
        assert receipt.status == "failed"
        assert [error.code for error in receipt.errors] == ["local_session_failed"]
        assert receipt.outputs
        assert all(output.ref["row_ids"] == row_ids[:1] for output in receipt.outputs)
    finally:
        project.close()


def test_fully_halted_generation_does_not_advertise_unrepresentable_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, sheet_id, row_ids = _seed_media_project(tmp_path / "backfill.frisket")
    state = {"halt": True, "code": "local_session_failed"}
    seen_specs: list[dict[str, Any]] = []

    @asynccontextmanager
    async def controlled_scope(
        spec: dict[str, Any],
        ctx: Any,
        *,
        expected_rows: int,
    ):
        del ctx, expected_rows
        seen_specs.append(dict(spec))
        if state["halt"]:
            raise RecipeInvocationHalt(
                str(state["code"]),
                "the reusable transcription session exited",
            )
        yield

    async def fake_faster_whisper(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        del self, path, spec
        return {
            "text": "recovered transcript",
            "segments": [{"start": 0.0, "end": 0.5, "text": "recovered transcript"}],
            "language": "en",
            "duration": 0.5,
        }

    monkeypatch.setattr(
        transcribe_engines, "transcription_execution_scope", controlled_scope
    )
    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", fake_faster_whisper
    )
    try:
        original = run_action_spec(
            project,
            _transcribe_action(
                sheet_id,
                row_ids,
                key="media-transcribe@sha256:original-halt",
            ),
            project_id=PROJECT_ID,
        )
        assert original.status == "failed"
        assert original.errors[0].code == "local_session_failed"

        assert "resumable" not in original.errors[0].details
        assert "resume_action" not in original.errors[0].details

        refused = run_action_spec(
            project,
            _backfill_action(
                sheet_id,
                "transcript",
                key="run-backfill@sha256:no-source-head",
            ),
            project_id=PROJECT_ID,
        )
        assert refused.status == "failed"
        assert refused.run_id is None
        assert [error.code for error in refused.errors] == ["run_required"]
        assert len(seen_specs) == 1
        assert _receipt(project, str(original.receipt_id)).status == "failed"
    finally:
        project.close()


def _seed_queued_media_project(
    client: TestClient, *, name: str, row_count: int = 1
) -> tuple[str, int, list[int]]:
    project_id = client.post("/api/projects", json={"name": name}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Episodes")
    columns = {"media": project.add_column(sheet_id, "media", type="audio")}
    blobs = [
        project.add_blob(
            f"RIFF0000WAVEfmt queued-{index}".encode(),
            filename=f"queued-{index}.wav",
            mime="audio/wav",
            metadata=owned_media_metadata_document(
                probe={"duration_seconds": 0.5, "kind": "audio"}
            ),
        )
        for index in range(row_count)
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "media": media_cell(
                    blob,
                    mime="audio/wav",
                    filename=f"queued-{index}.wav",
                )
            }
            for index, blob in enumerate(blobs)
        ],
        columns,
    )
    return project_id, sheet_id, row_ids


def test_queued_sandbox_teardown_keeps_typed_receipt_and_failed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(tmp_path / "queued-teardown"))
    project_id, sheet_id, row_ids = _seed_queued_media_project(
        client, name="Queued teardown", row_count=2
    )
    calls = 0

    async def teardown_second_row(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        nonlocal calls
        del self, path, spec
        calls += 1
        if calls == 2:
            raise SandboxTeardownError("owned process tree leaked secret diagnostic")
        return _transcript("queued committed transcript")

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", teardown_second_row
    )
    monkeypatch.setattr(
        transcribe_engines,
        "transcription_max_row_concurrency",
        lambda spec: 1,
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_transcribe_action(
            sheet_id,
            row_ids,
            key="media-transcribe@sha256:queued-sandbox-teardown",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    assert queued.run_id is not None
    assert queued.job_id is not None
    assert queued.receipt_id is not None

    drain_queue(client)

    assert calls == 2
    job = client.app.state.workspace.queue.get(queued.job_id)
    assert job is not None
    assert job.status == "failed"
    assert "secret diagnostic" in str(job.error)
    project = client.app.state.workspace.get(project_id)
    run = project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (queued.run_id,)
    ).fetchone()
    assert run["status"] == "cancelled"
    params = json.loads(run["params"])
    assert params["halted_code"] == "local_session_failed"
    assert "secret diagnostic" not in params["halted_reason"]
    assert (
        project.db.execute(
            "SELECT COUNT(DISTINCT row_id) FROM results WHERE run_id=?",
            (queued.run_id,),
        ).fetchone()[0]
        == 1
    )

    receipt = _receipt(project, queued.receipt_id)
    assert receipt.status == "failed"
    assert [error.code for error in receipt.errors] == ["local_session_failed"]
    assert receipt.errors[0].details["completed_rows"] == 1
    assert receipt.errors[0].details["total_rows"] == 2
    assert receipt.outputs
    assert all(output.ref["row_ids"] == row_ids[:1] for output in receipt.outputs)


def test_queued_internal_halt_can_be_recovered_by_direct_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = TestClient(create_app(tmp_path / "queued-backfill"))
    project_id, sheet_id, row_ids = _seed_queued_media_project(
        client, name="Queued backfill", row_count=2
    )
    calls = 0
    halt = True

    async def halt_then_recover(
        self: transcribe_engines.FasterWhisperAdapter,
        path: str,
        spec: dict[str, Any],
        *,
        should_cancel: Any = None,
    ) -> dict[str, Any]:
        nonlocal calls
        del self, path, spec
        calls += 1
        if halt and calls == 2:
            raise RecipeInvocationHalt(
                "local_session_failed", "session exited during queued work"
            )
        return _transcript(f"transcript {calls}")

    monkeypatch.setattr(
        transcribe_engines.FasterWhisperAdapter, "transcribe", halt_then_recover
    )
    monkeypatch.setattr(
        transcribe_engines,
        "transcription_max_row_concurrency",
        lambda spec: 1,
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_transcribe_action(
            sheet_id,
            row_ids,
            key="media-transcribe@sha256:queued-halt-before-backfill",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    assert queued.run_id is not None
    assert queued.receipt_id is not None
    drain_queue(client)

    project = client.app.state.workspace.get(project_id)
    original_receipt = _receipt(project, queued.receipt_id)
    assert original_receipt.status == "failed"
    assert original_receipt.errors[0].code == "local_session_failed"
    assert original_receipt.errors[0].details["completed_rows"] == 1
    assert calls == 2

    halt = False
    backfill_response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_backfill_action(
            sheet_id,
            "transcript",
            key="run-backfill@sha256:recover-queued-halt",
        ),
    )
    assert backfill_response.status_code == 200, backfill_response.text
    recovered = ActionResult.model_validate(backfill_response.json())
    assert recovered.status == "completed"
    assert recovered.run_id != queued.run_id
    assert recovered.outputs[0].ref["filled_row_ids"] == row_ids[1:]
    assert calls == 3

    original_run = project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (queued.run_id,)
    ).fetchone()
    assert original_run["status"] == "cancelled"
    assert json.loads(original_run["params"])["halted_code"] == "local_session_failed"
    successor_run = project.db.execute(
        "SELECT status, params FROM runs WHERE id=?", (recovered.run_id,)
    ).fetchone()
    assert successor_run["status"] == "completed"
    successor_params = json.loads(successor_run["params"])
    assert "halted_code" not in successor_params
    assert "halted_reason" not in successor_params
    assert _receipt(project, queued.receipt_id).status == "failed"
    assert _receipt(project, str(recovered.receipt_id)).status == "completed"


@pytest.mark.parametrize(
    ("halted_code", "expected_status", "expected_error"),
    [
        ("local_engine_busy", "failed", "local_engine_busy"),
        (None, "cancelled", None),
    ],
)
def test_queued_terminal_reentry_projects_halt_or_operator_cancel(
    tmp_path: Path,
    halted_code: str | None,
    expected_status: str,
    expected_error: str | None,
) -> None:
    client = TestClient(create_app(tmp_path / f"queued-{expected_status}"))
    project_id, sheet_id, row_ids = _seed_queued_media_project(
        client, name=f"Queued {expected_status}"
    )
    response = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_transcribe_action(
            sheet_id,
            row_ids,
            key=f"media-transcribe@sha256:queued-{expected_status}",
        ),
    )
    assert response.status_code == 200, response.text
    queued = ActionResult.model_validate(response.json())
    assert queued.status == "queued"
    assert queued.run_id is not None
    assert queued.receipt_id is not None

    project = client.app.state.workspace.get(project_id)
    run = project.db.execute(
        "SELECT params FROM runs WHERE id=?", (queued.run_id,)
    ).fetchone()
    params = json.loads(run["params"])
    if halted_code is not None:
        params["halted_code"] = halted_code
        params["halted_reason"] = "another local transcription is still finishing"
    project.db.execute(
        "UPDATE runs SET status='cancelled', finished_at=datetime('now'), params=? "
        "WHERE id=?",
        (json.dumps(params, sort_keys=True), queued.run_id),
    )
    # Simulate the completed terminal projection, including the generation
    # seal required by receipt capture, before redelivering the queued job.
    from frisket.engine.store.result_generations import ResultGenerationStore

    generations = ResultGenerationStore(project)
    bindings = generations.bindings_for_run(queued.run_id)
    generations.seal(
        queued.run_id,
        [binding.column_id for binding in bindings],
        claim_token=bindings[0].claim_token,
        terminal_disposition="cancelled",
        commit=False,
    )
    project.db.commit()

    # The worker sees an already-terminal run and must reconstruct the final
    # receipt from durable metadata without executing a transcription row.
    drain_queue(client)

    receipt = _receipt(project, queued.receipt_id)
    assert receipt.status == expected_status, client.app.state.workspace.queue.get(
        queued.job_id
    ).error
    if expected_error is None:
        assert receipt.errors == []
    else:
        assert [error.code for error in receipt.errors] == [expected_error]
        assert "resumable" not in receipt.errors[0].details
        assert "resume_action" not in receipt.errors[0].details
    if expected_error is not None:
        assert receipt.outputs == []
    else:
        assert receipt.outputs
    assert all(item.ref.get("row_ids") == [] for item in receipt.outputs)
