"""Installed native actions retain the shared output and table semantics."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from frisket.actions.types import ActionRequest
from frisket.authoring.plugin_registry import _reset_default_registry_for_tests
from frisket.authoring.workbench import plugin_runtime, plugin_runtime_status
from frisket.authoring.workbench.installed_actions import resolve_installed_action
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store.receipts import ReceiptStore
from frisket.plugins.manifest_generate import generate_manifest_text
from frisket.server.app import create_app


SOURCE = """
from __future__ import annotations

import io

from pydantic import BaseModel

from frisket.actions.core import ActionCategory, action, create_sheet, map_batch
from frisket.actions.types import (
    ActionParams,
    ColumnRef,
    ImportBlobStager,
    ListTableReader,
    ListTableSource,
    Outcome,
    RowResult,
    Rows,
    StagedFile,
    TableResult,
    TableRow,
)
from frisket.plugins.sdk import Plugin


batch_calls = []


class BatchParams(ActionParams):
    text: ColumnRef[str]
    include_length: bool = False


class BatchOutput(BaseModel):
    upper: Outcome[str]
    length: Outcome[int] | None = None


def batch(params: BatchParams, rows: Rows) -> dict[int, RowResult[BatchOutput]]:
    batch_calls.append(tuple((row_id, params.text.read(row)) for row_id, row in rows.items()))
    return {
        row_id: RowResult(
            output=BatchOutput(
                upper=Outcome.ok(
                    params.text.read(row).upper(),
                    confidence=0.8,
                    justification="whole installed batch",
                ),
                **(
                    {"length": Outcome.ok(len(params.text.read(row)))}
                    if params.include_length
                    else {}
                ),
            )
        )
        for row_id, row in rows.items()
    }


def batch_outputs(params: BatchParams) -> tuple[str, ...]:
    return ("upper", "length") if params.include_length else ("upper",)


class AttachmentParams(ActionParams):
    source: ListTableSource


class AttachmentOutput(BaseModel):
    attachment: StagedFile


def attachments(
    params: AttachmentParams,
    tables: ListTableReader,
    blobs: ImportBlobStager,
) -> TableResult[AttachmentOutput]:
    rows = []
    for item in tables.read(params.source):
        value = str(item.value)
        handle = blobs.stage(
            io.BytesIO(value.encode()),
            filename=f"{value}.txt",
            mime="text/plain",
        )
        rows.append(
            TableRow(
                output=AttachmentOutput(attachment=handle),
                sources=(item.source,),
                parent=item.source,
            )
        )
    return TableResult(rows=rows)


plugin = Plugin(
    id="example.parity",
    version="1.0.0",
    auto_enable=True,
    capabilities=["plugin:trusted_local_backend"],
    actions=(
        action(
            name="batch",
            title="Batch",
            description="Process one admitted row set.",
            category=ActionCategory.CONVERT,
            run=map_batch(batch, active_outputs=batch_outputs),
        ),
        action(
            name="attachments",
            title="Attachments",
            description="Materialize admitted list items as files.",
            category=ActionCategory.SOURCES,
            run=create_sheet(attachments),
        ),
    ),
)
"""


@pytest.fixture
def installed(tmp_path, monkeypatch):
    bundled = tmp_path / "bundled"
    package = bundled / "example.parity"
    package.mkdir(parents=True)
    (package / "plugin.py").write_text(SOURCE)
    (package / "plugin.json").write_text(generate_manifest_text(package))
    monkeypatch.setattr(plugin_runtime, "_bundled_plugins_root", lambda: bundled)
    monkeypatch.setattr(plugin_runtime_status, "_bundled_plugins_root", lambda: bundled)
    _reset_default_registry_for_tests()
    app = create_app(
        tmp_path / "workspace",
        enable_provider_config=False,
        edition="team",
        serve_spa=False,
    )
    with TestClient(app):
        project_id = app.state.workspace.create("native output parity")["id"]
        project = app.state.workspace.get(project_id)
        sheet_id = project.add_sheet("Sources")
        text_id = project.add_column(sheet_id, "Name", "text")
        list_id = project.add_column(sheet_id, "Items", "json")
        row_ids = project.add_rows(
            sheet_id,
            [
                {"Name": "Ada", "Items": ["alpha", "beta"]},
                {"Name": "Grace", "Items": ["gamma"]},
            ],
            {"Name": text_id, "Items": list_id},
        )
        yield project, project_id, sheet_id, list_id, row_ids
    _reset_default_registry_for_tests()


def _batch_request(sheet_id: int, *, include_length: bool, key: str) -> dict:
    return {
        "action_id": "example.parity.batch",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"text": "Name", "include_length": include_length},
        "output_names": {
            "upper": f"Upper {key}",
            **({"length": "Length"} if include_length else {}),
        },
        "idempotency_key": f"batch-{key}",
    }


def test_installed_map_batch_keeps_whole_set_conditional_outputs_and_outcome(
    installed,
):
    project, project_id, sheet_id, _list_id, row_ids = installed
    plain = run_action_spec(
        project,
        _batch_request(sheet_id, include_length=False, key="plain"),
        project_id=project_id,
    )
    expanded = run_action_spec(
        project,
        _batch_request(sheet_id, include_length=True, key="expanded"),
        project_id=project_id,
    )
    assert plain.status == expanded.status == "completed", [
        *plain.errors,
        *expanded.errors,
    ]

    action, _binding = resolve_installed_action(project, "example.parity.batch")
    calls = action.definition.run.handler.__globals__["batch_calls"]
    assert calls == [
        ((row_ids[0], "Ada"), (row_ids[1], "Grace")),
        ((row_ids[0], "Ada"), (row_ids[1], "Grace")),
    ]
    columns = {column["name"]: column["id"] for column in project.columns(sheet_id)}
    assert "Length" not in {
        output.name for output in plain.outputs if output.column_id is not None
    }
    assert project.get_values(sheet_id, columns["Upper plain"]) == {
        row_ids[0]: "ADA",
        row_ids[1]: "GRACE",
    }
    assert project.get_values(sheet_id, columns["Length"]) == {
        row_ids[0]: 3,
        row_ids[1]: 5,
    }
    outcomes = project.db.execute(
        "SELECT confidence, justification FROM results "
        "WHERE run_id=? AND column_id=? ORDER BY row_id",
        (plain.run_id, columns["Upper plain"]),
    ).fetchall()
    assert [tuple(row) for row in outcomes] == [
        (0.8, "whole installed batch"),
        (0.8, "whole installed batch"),
    ]


def test_installed_create_sheet_lowers_files_and_keeps_admitted_parent_lineage(
    installed,
):
    project, project_id, sheet_id, list_id, row_ids = installed
    result = run_action_spec(
        project,
        ActionRequest(
            action_id="example.parity.attachments",
            scope={"kind": "project"},
            params={
                "source": {
                    "kind": "column",
                    "sheet_id": sheet_id,
                    "column_id": list_id,
                }
            },
            sheet_name="Attachments",
            idempotency_key="attachments",
        ).model_dump(mode="json"),
        project_id=project_id,
    )
    assert result.status == "completed", result.errors
    output = result.outputs[0].ref
    assert output["row_count"] == 3
    values = list(
        project.get_values(output["sheet_id"], output["columns"]["attachment"]).values()
    )
    assert values == [
        {
            "blob": hashlib.sha256(value.encode()).hexdigest(),
            "filename": f"{value}.txt",
            "mime": "text/plain",
        }
        for value in ("alpha", "beta", "gamma")
    ]
    parent_sheet = project.db.execute(
        "SELECT parent_sheet_id FROM sheets WHERE id=?", (output["sheet_id"],)
    ).fetchone()[0]
    parent_rows = project.db.execute(
        "SELECT parent_row_id FROM rows WHERE sheet_id=? ORDER BY position",
        (output["sheet_id"],),
    ).fetchall()
    assert parent_sheet == sheet_id
    assert [row["parent_row_id"] for row in parent_rows] == [
        row_ids[0],
        row_ids[0],
        row_ids[1],
    ]
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    assert [item.ref["kind"] for item in receipt.inputs] == ["list_table_read"]
    assert receipt.inputs[0].ref["source_row_ids"] == row_ids
