"""Explicit ``export.sheet_jsonl`` and ``export.sheet_parquet`` v1 actions.

Both reuse the shared dataset export plan + media policy, write typed artifacts
(JSONL one object per row; Parquet via pyarrow with stable schema metadata),
and carry the same artifact receipt refs / idempotency-replay as CSV.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
from fastapi.testclient import TestClient

from frisket.contracts.action import (
    ActionResult,
    Receipt,
)
from frisket.actions.system import root_action_catalog, validate_root_action


def _seed(client: TestClient) -> tuple[str, int, dict[str, int], list[int]]:
    pid = client.post("/api/projects", json={"name": "Analytics"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    columns = {
        "name": project.add_column(sheet_id, "name"),
        "count": project.add_column(sheet_id, "count", type="integer"),
        "ok": project.add_column(sheet_id, "ok", type="boolean"),
        "meta": project.add_column(sheet_id, "meta", type="json"),
        "media": project.add_column(sheet_id, "media", type="audio"),
    }
    digest = project.add_blob(
        b"audio-bytes" * 4,
        filename="clip.mp3",
        mime="audio/mpeg",
        metadata=owned_media_metadata_document(probe={"duration_seconds": 4.0}),
    )
    rows = project.add_rows(
        sheet_id,
        [
            {
                "name": "Ada",
                "count": 2,
                "ok": True,
                "meta": {"b": 2, "a": 1},
                "media": {"blob": digest},
            },
            {
                "name": "Grace",
                "count": 5,
                "ok": False,
                "meta": {"k": "v"},
                "media": None,
            },
        ],
        columns,
    )
    return pid, sheet_id, columns, rows


def _action(kind: str, sheet_id: int, path: Path, *, key: str) -> dict[str, Any]:
    return {
        "action_id": kind,
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(path)},
        },
        "idempotency_key": key,
    }


def _run(client: TestClient, pid: str, action: dict[str, Any]) -> ActionResult:
    resp = client.post(f"/api/projects/{pid}/actions/v1/run", json=action)
    assert resp.status_code == 200, resp.text
    return ActionResult.model_validate(resp.json())


def test_catalog_exposes_explicit_jsonl_and_parquet_actions() -> None:
    catalog = root_action_catalog()
    kinds = {entry.kind for entry in catalog.actions}
    assert {"export.sheet_jsonl", "export.sheet_parquet"} <= kinds
    # explicit actions, not a polymorphic widening of export.sheet_csv
    for kind, target in (
        ("export.sheet_jsonl", "jsonl"),
        ("export.sheet_parquet", "parquet"),
    ):
        entry = next(e for e in catalog.actions if e.kind == kind)
        assert entry.receipt_policy == "writes_receipt"
        assert entry.ui_hints["export_target"]["destination_kind"] == target


def test_export_sheet_jsonl_writes_typed_objects(
    client: TestClient, tmp_path: Path
) -> None:
    pid, sheet_id, _cols, rows = _seed(client)
    out = tmp_path / "rows.jsonl"
    result = _run(client, pid, _action("export.sheet_jsonl", sheet_id, out, key="j@1"))
    assert result.status == "completed"
    ref = next(o for o in result.outputs if o.kind == "export").ref
    assert ref["format"] == "jsonl"
    assert ref["sha256"].startswith("sha256:")
    assert ref["byte_count"] == out.stat().st_size

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    # typed, NOT stringified
    assert first["count"] == 2 and isinstance(first["count"], int)
    assert first["ok"] is True
    assert first["meta"] == {"a": 1, "b": 2}  # JSON value preserved, not CSV string
    # media flattened to reference fields, hash not bytes/path
    assert first["media.blob"] == _seed_digest(client, pid, sheet_id)
    assert first["media.mime_type"] == "audio/mpeg"
    second = json.loads(lines[1])
    assert second["media.blob"] is None


def _seed_digest(client: TestClient, pid: str, sheet_id: int) -> str:
    project = client.app.state.workspace.get(pid)
    row = project.db.execute("SELECT hash FROM blobs LIMIT 1").fetchone()
    return row["hash"]


def test_export_sheet_parquet_writes_typed_table(
    client: TestClient, tmp_path: Path
) -> None:
    pid, sheet_id, _cols, _rows = _seed(client)
    out = tmp_path / "rows.parquet"
    result = _run(
        client,
        pid,
        _action("export.sheet_parquet", sheet_id, out, key="p@1"),
    )
    assert result.status == "completed"
    ref = next(o for o in result.outputs if o.kind == "export").ref
    assert ref["format"] == "parquet"
    assert ref["byte_count"] == out.stat().st_size

    table = pq.read_table(out)
    data = table.to_pydict()
    assert data["count"] == [2, 5]  # native ints
    assert data["ok"] == [True, False]
    assert "media.blob" in data and "media.mime_type" in data
    # stable schema metadata
    meta = table.schema.metadata or {}
    assert meta.get(b"frisket.export_plan.schema_version") == b"frisket.export_plan.v1"
    assert meta.get(b"frisket.sheet_id") == str(sheet_id).encode()

    project = client.app.state.workspace.get(pid)
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.outputs[0].name == "sheet_parquet"
    assert receipt.outputs[0].ref["format"] == "parquet"


def test_parquet_stringifies_heterogeneous_json_column(
    client: TestClient, tmp_path: Path
) -> None:
    # a json column holding mixed scalar/dict values must not defeat Arrow type
    # inference; it is serialized to a stable string column instead.
    pid = client.post("/api/projects", json={"name": "Mixed"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    sheet_id = project.add_sheet("rows")
    cols = {"meta": project.add_column(sheet_id, "meta", type="json")}
    project.add_rows(
        sheet_id,
        [{"meta": 5}, {"meta": "hi"}, {"meta": {"a": 1}}],
        cols,
    )
    out = tmp_path / "mixed.parquet"
    result = _run(
        client,
        pid,
        _action("export.sheet_parquet", sheet_id, out, key="mix@1"),
    )
    assert result.status == "completed"
    table = pq.read_table(out)
    meta = table.to_pydict()["meta"]
    assert meta == ["5", '"hi"', '{"a": 1}']  # JSON text, stable string column


def test_jsonl_validation_rejects_redundant_format(
    client: TestClient, tmp_path: Path
) -> None:
    action = _action("export.sheet_jsonl", 1, tmp_path / "x.jsonl", key="bad@1")
    action["params"]["format"] = "csv"
    validation = validate_root_action(action)
    assert validation.ok is False
    assert validation.error.code == "invalid_action_request"


def test_jsonl_idempotent_replay_and_conflict(
    client: TestClient, tmp_path: Path
) -> None:
    pid, sheet_id, _cols, _rows = _seed(client)
    out = tmp_path / "rows.jsonl"
    first = _run(client, pid, _action("export.sheet_jsonl", sheet_id, out, key="rep@1"))
    replay = _run(
        client, pid, _action("export.sheet_jsonl", sheet_id, out, key="rep@1")
    )
    assert replay.receipt_id == first.receipt_id

    # same key, different params -> conflict
    conflict = client.post(
        f"/api/projects/{pid}/actions/v1/run",
        json=_action(
            "export.sheet_jsonl",
            sheet_id,
            tmp_path / "other.jsonl",
            key="rep@1",
        ),
    )
    assert conflict.status_code == 409
    assert conflict.json()["errors"][0]["code"] == "idempotency_conflict"

    project = client.app.state.workspace.get(pid)
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.outputs[0].name == "sheet_jsonl"
    assert receipt.outputs[0].ref["format"] == "jsonl"
    assert receipt.outputs[0].ref["kind"] == "export_artifact"
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "exported_sheet",
        "exported_rows",
    }
