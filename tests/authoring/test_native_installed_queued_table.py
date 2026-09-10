"""Installed reader-backed tables survive queue admission and reconstruction."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.engine.executor.transcript_read import AdmittedTranscriptReader
from frisket.engine.jobs.worker import Worker
from frisket.engine.store.receipts import ReceiptStore
from frisket.plugins.manifest_generate import generate_manifest_text
from frisket.server.app import create_app


SOURCE = """
from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.transcript_types import TranscriptColumn, TranscriptReader, TranscriptSelection
from frisket.actions.types import ActionParams, DynamicTableResult
from frisket.plugins.sdk import Plugin

class Params(ActionParams):
    transcript: TranscriptColumn
    selection: TranscriptSelection

def excerpts(params: Params, reader: TranscriptReader) -> DynamicTableResult:
    return reader.read(params.transcript, params.selection)

plugin = Plugin(
    id="example.queued_table",
    version="1.0.0",
    auto_enable=True,
    capabilities=["plugin:trusted_local_backend"],
    actions=(
        action(
            name="excerpts",
            title="Excerpts",
            description="Create transcript excerpts",
            category=ActionCategory.CONVERT,
            run=create_sheet(excerpts),
        ),
    ),
)
"""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    root = tmp_path / "bundled"
    package = root / "example.queued_table"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(SOURCE)
    (package / "plugin.json").write_text(generate_manifest_text(package))
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: root)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: root)
    _reset_default_registry_for_tests()

    calls = []

    def read(reader, transcript, selection):
        from frisket.actions.types import (
            DynamicOutput,
            DynamicTableResult,
            RowSource,
            TableColumn,
            TableRow,
        )

        calls.append((transcript.name, selection.kind))
        source = RowSource(
            sheet_id=reader.scope.sheet_id, row_id=reader.scope.row_ids[0]
        )
        reader.sources.add(source)
        return DynamicTableResult(
            schema=(TableColumn("excerpt", "text"),),
            rows=(
                TableRow(
                    output=DynamicOutput({"excerpt": "An admitted excerpt."}),
                    sources=(source,),
                    parent=source,
                ),
            ),
        )

    monkeypatch.setattr(AdmittedTranscriptReader, "read", read)
    app = create_app(
        tmp_path / "workspace",
        enable_provider_config=False,
        edition="team",
        serve_spa=False,
    )
    with TestClient(app) as client:
        project_id = app.state.workspace.create("queued native table")["id"]
        project = app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Media")
        transcript_id = project.add_column(
            sheet_id, "Transcript", "timestamped_transcript", ai_generated=True
        )
        row_id = project.add_rows(
            sheet_id,
            [{"Transcript": "Source transcript."}],
            {"Transcript": transcript_id},
        )[0]
        yield client, project, project_id, sheet_id, row_id, calls
    _reset_default_registry_for_tests()


def _request(sheet_id, row_id, *, key="installed-queued-table"):
    return {
        "action_id": "example.queued_table.excerpts",
        "scope": {
            "kind": "sheet_rows",
            "sheet_id": sheet_id,
            "row_ids": [row_id],
        },
        "params": {
            "transcript": "Transcript",
            "selection": {
                "kind": "draft_range",
                "start_ms": 0,
                "end_ms": 1_000,
            },
        },
        "sheet_name": "Excerpt output",
        "idempotency_key": key,
    }


def _queue(client, project_id, body):
    response = client.post(f"/api/projects/{project_id}/actions/v1/run", json=body)
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["status"] == "queued", queued
    return queued


def test_installed_reader_table_queues_and_worker_reconstructs(installed):
    client, project, project_id, sheet_id, row_id, calls = installed
    queued = _queue(client, project_id, _request(sheet_id, row_id))
    assert calls == []

    workspace = client.app.state.workspace
    assert Worker(
        workspace.queue,
        workspace.registry,
        worker_id="installed-queued-table",
    ).run_once()
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "completed", receipt.model_dump()
    assert calls == [("Transcript", "draft_range")]

    output_sheet = project.db.execute(
        "SELECT id FROM sheets WHERE name='Excerpt output'"
    ).fetchone()["id"]
    column = project.columns(output_sheet)[0]
    assert column["name"] == "excerpt"
    assert list(project.get_values(output_sheet, column["id"]).values()) == [
        "An admitted excerpt."
    ]


def test_worker_refuses_installed_reader_table_disabled_after_queue(installed):
    client, project, project_id, sheet_id, row_id, calls = installed
    queued = _queue(
        client,
        project_id,
        _request(sheet_id, row_id, key="installed-disabled-before-worker"),
    )
    plugin_runtime_status._record_backend_activation_executable_handlers_grant(
        project,
        plugin_id="example.queued_table",
        executable_handlers_allowed=False,
    )

    workspace = client.app.state.workspace
    assert Worker(
        workspace.queue,
        workspace.registry,
        worker_id="installed-disabled-table",
    ).run_once()
    receipt = ReceiptStore(project).parsed_by_id(queued["receipt_id"])
    assert receipt.status == "failed", receipt.model_dump()
    assert calls == []
    assert (
        project.db.execute(
            "SELECT COUNT(*) FROM sheets WHERE name='Excerpt output'"
        ).fetchone()[0]
        == 0
    )
