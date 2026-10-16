from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.ai.models.gateway_config import ModelsGatewayConnection
from frisket.contracts.action import ActionResult
from frisket.engine.store.execution_routes import RouteStore
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.provider import ExactExecutionMatch
from frisket.server.services import action_runs as action_runs_module
from frisket.server.services.action_runs import ActionRunService
from frisket.server.workspace import Workspace


LOCAL_PARAKEET = ExactExecutionMatch(
    target_id="local-onnx",
    capability="transcribe",
    engine="parakeet-tdt",
)


def _workspace_with_audio(root: Path) -> tuple[Workspace, int, list[int]]:
    workspace = Workspace(
        root,
        enable_local_model_pull=False,
        models_gateway_connection_resolver=lambda: ModelsGatewayConnection(
            origin="https://models.example",
            token="test-token",
            source="stored",
        ),
    )
    workspace.create("Audio", project_id="audio")
    project = workspace.get("audio")
    sheet_id = project.add_sheet("Files")
    media_id = project.add_column(sheet_id, "media", type="audio")
    digest = project.add_blob(
        b"RIFF0000WAVEfmt ", filename="sample.wav", mime="audio/wav"
    )
    row_ids = project.add_rows(
        sheet_id,
        [{"media": media_cell(digest, mime="audio/wav", filename="sample.wav")}],
        {"media": media_id},
    )
    return workspace, sheet_id, row_ids


def _transcribe_body(sheet_id: int, row_ids: list[int]) -> dict[str, Any]:
    return {
        "action_id": "media.transcribe",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id, "row_ids": row_ids},
        "params": {"source": "media", "engine": "parakeet-tdt"},
        "output_names": {
            "text": "transcript",
            "segments": "transcript_segments",
        },
        "idempotency_key": "automatic-transcription@1",
    }


def _prefer_gateway(monkeypatch) -> None:
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_runtime_present", lambda: True
    )
    monkeypatch.setattr(
        "frisket.execution.definitions.parakeet_artifacts_present", lambda: True
    )
    original = StaticExecutionTargetProvider.selection_rank

    def gateway_first(self, target_id: str, capability: str, engine: str) -> int:
        if target_id == "models-gateway" and engine == "parakeet-tdt":
            return -1
        return original(self, target_id, capability, engine)

    monkeypatch.setattr(StaticExecutionTargetProvider, "selection_rank", gateway_first)


def test_queued_action_persists_required_local_target_and_replays_it(
    tmp_path: Path, monkeypatch
) -> None:
    _prefer_gateway(monkeypatch)
    workspace, sheet_id, row_ids = _workspace_with_audio(tmp_path / "workspace")
    service = ActionRunService(workspace)

    response = service.run_action(
        "audio",
        _transcribe_body(sheet_id, row_ids),
        required_execution_target=LOCAL_PARAKEET,
    )

    assert response.status_code == 200, response.payload
    assert response.payload["status"] == "queued"
    project = workspace.get("audio")
    run_id = int(response.payload["run_id"])
    route, _promises = RouteStore.for_run(project, run_id).head()
    assert route.target_snapshot["target_id"] == "local-onnx"

    replay = service.recover_prepared_action(
        "audio", receipt_id=str(response.payload["receipt_id"])
    )

    assert replay is not None
    assert replay.payload["run_id"] == run_id
    replay_route, _promises = RouteStore.for_run(project, run_id).head()
    assert replay_route.target_snapshot["target_id"] == "local-onnx"


def test_direct_action_receives_same_required_target_composition(
    tmp_path: Path, monkeypatch
) -> None:
    _prefer_gateway(monkeypatch)
    workspace, sheet_id, row_ids = _workspace_with_audio(tmp_path / "workspace")
    observed: list[tuple[tuple[str, tuple[str, ...]], ...]] = []

    monkeypatch.setattr(action_runs_module, "queue_v1_action_run", lambda *a, **k: None)

    def capture_direct(project, body, **kwargs):
        composition = kwargs["deps"].execution_composition
        observed.append(
            tuple(
                (
                    target.id,
                    tuple(
                        support.engine
                        for support in target.engines
                        if support.capability == "transcribe"
                    ),
                )
                for target in composition.resolution_targets()
            )
        )
        return ActionResult(
            action={"kind": "media.transcribe", "action_id": "act_test"},
            status="completed",
            project_id="audio",
        )

    monkeypatch.setattr(action_runs_module, "run_action_spec", capture_direct)

    response = ActionRunService(workspace).run_action(
        "audio",
        _transcribe_body(sheet_id, row_ids),
        required_execution_target=LOCAL_PARAKEET,
    )

    assert response.status_code == 200, response.payload
    assert observed == [(("local-onnx", ("parakeet-tdt",)),)]
