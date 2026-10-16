from __future__ import annotations

from fastapi.testclient import TestClient
from dataclasses import dataclass
from dataclasses import replace

from frisket.server.app import create_app
from frisket.server.services import search_preparation as preparation_module
from frisket.server.services.search_preparation import SearchPreparationService
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class _Requirement:
    setup_ref: str = "engine-setup:rapidocr.local@1"
    capability: str = "ocr"
    display_name: str = "RapidOCR"
    runtime_ready: bool = True
    weights_ready: bool = False
    download_bytes_if_missing: int = 123
    license: str = "Apache-2.0"
    source_url: str = "https://example.invalid/rapidocr"
    runtime_remediation: str | None = None
    artifact_refs: tuple[str, ...] = ("sha256:fixture",)
    download_size_basis: str = "exact_checksum"
    runtime_installable: bool = False


def test_explicit_preparation_is_reconnectable_and_double_click_safe(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Preparation"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    name_column = project.add_column(sheet_id, "Name", type="text")
    project.add_rows(sheet_id, [{"Name": "ordinary text"}], {"Name": name_column})

    body = {"sheet_id": sheet_id, "row_ids": project.visible_row_ids(sheet_id)}
    first = client.post(f"/api/projects/{project_id}/search-preparation", json=body)
    second = client.post(f"/api/projects/{project_id}/search-preparation", json=body)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]
    # Project writes leave lexical index work pending; the batch remains
    # preparing until the normal index worker makes the content query-visible.
    assert first.json()["status"] == "preparing"

    listed = client.get(f"/api/projects/{project_id}/search-preparation")
    assert listed.status_code == 200
    assert [batch["id"] for batch in listed.json()["batches"]] == [first.json()["id"]]
    fetched = client.get(
        f"/api/projects/{project_id}/search-preparation/{first.json()['id']}"
    )
    assert fetched.status_code == 200
    assert fetched.json() == first.json()


def test_explicit_preparation_rejects_rows_outside_visible_scope(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: ())
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = client.post("/api/projects", json={"name": "Preparation"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    project.add_column(sheet_id, "Name", type="text")

    response = client.post(
        f"/api/projects/{project_id}/search-preparation",
        json={"sheet_id": sheet_id, "row_ids": [999]},
    )
    assert response.status_code == 404


def test_missing_ocr_setup_never_starts_before_exact_consent(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(preparation_module, "_requirements", lambda: (_Requirement(),))
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Images")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    setup_calls: list[tuple[str, str | None]] = []
    service = SearchPreparationService(
        workspace,
        setup_start=lambda ref, actor: setup_calls.append((ref, actor)) or 1,
    )

    batch = service.create(
        project_id,
        sheet_id=sheet_id,
        row_ids=None,
        force_ocr=False,
    )
    assert batch.status == "needs_consent"
    assert setup_calls == []
    decided = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=["engine-setup:rapidocr.local@1"],
        may_setup_models=True,
        initiated_by="owner-1",
    )
    assert decided.status == "preparing"
    assert setup_calls == [("engine-setup:rapidocr.local@1", "owner-1")]


def test_eligible_ocr_consent_is_not_blocked_by_unavailable_transcription_runtime(
    tmp_path, monkeypatch
) -> None:
    transcription = replace(
        _Requirement(),
        setup_ref="engine-setup:parakeet-tdt.local-onnx@1",
        capability="transcription",
        display_name="Parakeet",
        runtime_ready=False,
        runtime_remediation="Install the standard local audio runtime.",
        artifact_refs=("hf:parakeet-fixture",),
    )
    monkeypatch.setattr(
        preparation_module,
        "_requirements",
        lambda: (_Requirement(), transcription),
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Mixed media")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    audio_column = project.add_column(sheet_id, "Audio", type="audio")
    image = project.add_blob(b"image", filename="scan.png", mime="image/png")
    audio = project.add_blob(b"audio", filename="clip.wav", mime="audio/wav")
    project.add_rows(
        sheet_id,
        [
            {
                "Image": {"blob": image, "filename": "scan.png", "mime": "image/png"},
                "Audio": {"blob": audio, "filename": "clip.wav", "mime": "audio/wav"},
            }
        ],
        {"Image": image_column, "Audio": audio_column},
    )
    setup_calls = []
    service = SearchPreparationService(
        workspace,
        setup_start=lambda ref, actor: setup_calls.append((ref, actor)) or 1,
    )

    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    assert batch.status == "needs_consent"
    assert batch.can_setup
    assert {model.purpose: model.can_setup for model in batch.models} == {
        "read_text": True,
        "transcribe": False,
    }
    decided = service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[_Requirement.setup_ref],
        may_setup_models=True,
        initiated_by="owner-1",
    )
    assert decided.status == "preparing"
    assert setup_calls == [(_Requirement.setup_ref, "owner-1")]


def test_recovery_observes_setup_completed_by_another_process(
    tmp_path, monkeypatch
) -> None:
    requirements = [_Requirement()]
    monkeypatch.setattr(
        preparation_module, "_requirements", lambda: tuple(requirements)
    )
    workspace = Workspace(tmp_path / "workspace")
    project_id = workspace.create("Images")["id"]
    project = workspace.get(project_id)
    sheet_id = project.add_sheet("Files")
    image_column = project.add_column(sheet_id, "Image", type="image")
    digest = project.add_blob(b"image", filename="scan.png", mime="image/png")
    project.add_rows(
        sheet_id,
        [{"Image": {"blob": digest, "filename": "scan.png", "mime": "image/png"}}],
        {"Image": image_column},
    )
    service = SearchPreparationService(workspace, setup_start=lambda _ref, _actor: 1)
    batch = service.create(project_id, sheet_id=sheet_id, row_ids=None, force_ocr=False)
    service.decide(
        project_id,
        batch.id,
        plan_hash=batch.plan_hash,
        decision="prepare",
        selected_setup_refs=[_Requirement.setup_ref],
        may_setup_models=True,
        initiated_by="owner-1",
    )

    # A separate CLI worker makes the shared setup fact ready. A fresh server
    # coordinator observes that durable fact without any in-process callback.
    requirements[0] = replace(requirements[0], weights_ready=True)
    SearchPreparationService(workspace, setup_start=lambda _ref, _actor: 2).recover()

    resumed = service.get(project_id, batch.id)
    assert resumed.status == "preparing"
    assert len(resumed.jobs) == 1
    assert resumed.jobs[0].status == "queued"
