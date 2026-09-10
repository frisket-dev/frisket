from __future__ import annotations

import json
import gzip
from pathlib import Path
from typing import Any

import pytest

import frisket.engine.receipt_index as receipt_index
import frisket.operability.trace as trace
from frisket.server.exports.work_log import build_work_log_payload
from frisket.engine.store import Project


ROOT = Path(__file__).resolve().parents[2]


def _write_trace_sidecar(bundle_path: Path, run_id: int) -> Path:
    sidecar = trace.trace_path(bundle_path, run_id)
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(sidecar, "wt", encoding="utf-8") as stream:
        stream.write(
            "\n".join(
                [
                    json.dumps(
                        {
                            "kind": "meta",
                            "run_id": run_id,
                            "trace_id": "trace-index",
                            "action_kind": "map.classify",
                            "model": "anthropic/test",
                            "created_at": "2026-06-19T10:00:00Z",
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": 10,
                            "trace_id": "trace-index",
                            "raw_response": "old",
                            "error": "transient",
                            "retries": [{"status": 429, "error": "rate limited"}],
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": 10,
                            "trace_id": "trace-index",
                            "raw_response": "latest",
                            "retries": [{"status": 200}],
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": 11,
                            "trace_id": "trace-index",
                            "error": "still broken",
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "compacted_row",
                            "row_id": 12,
                            "reason": "retention_policy",
                        }
                    ),
                    json.dumps(
                        {
                            "kind": "row",
                            "row_id": 999,
                            "trace_id": "trace-index",
                            "error": "unrelated row that should stay indexed only",
                        }
                    ),
                    "",
                ]
            )
        )
    return sidecar


def _receipt_body(
    *,
    op_ids: list[int],
    evidence_kind: str,
    output_kind: str,
    export_path: str | None = None,
) -> dict[str, Any]:
    outputs = [{"ref": {"kind": output_kind}}]
    if export_path is not None:
        outputs.append(
            {
                "ref": {
                    "kind": "export_artifact",
                    "export_kind": "work_log",
                    "format": "markdown",
                    "path": export_path,
                    "byte_count": 10,
                    "sha256": "sha256:abc",
                }
            }
        )
    return {
        "op_ids": op_ids,
        "inputs": [{"ref": {"kind": "source_cell"}}],
        "outputs": outputs,
        "evidence": [{"ref": {"kind": evidence_kind}}],
    }


def _insert_receipt(
    project: Project,
    *,
    receipt_id: str,
    action_kind: str,
    body: dict[str, Any],
    created_at: str,
) -> None:
    project.db.execute(
        "INSERT INTO receipts "
        "(id, action_kind, status, body, created_at) "
        "VALUES (?, ?, 'completed', ?, ?)",
        (receipt_id, action_kind, json.dumps(body), created_at),
    )
    project.db.commit()


def _receipt_project(tmp_path: Path) -> Project:
    project = Project.create(tmp_path / "receipt-index.frisket", name="Receipts")
    _insert_receipt(
        project,
        receipt_id="wf_import_a",
        action_kind="import.csv",
        body=_receipt_body(
            op_ids=[1], evidence_kind="source_file", output_kind="sheet"
        ),
        created_at="2026-06-19 10:00:00",
    )
    _insert_receipt(
        project,
        receipt_id="wf_import_b",
        action_kind="import.csv",
        body=_receipt_body(
            op_ids=[2], evidence_kind="source_file", output_kind="sheet"
        ),
        created_at="2026-06-19 10:01:00",
    )
    _insert_receipt(
        project,
        receipt_id="wf_export",
        action_kind="export.work_log",
        body=_receipt_body(
            op_ids=[3],
            evidence_kind="exported_receipts",
            output_kind="artifact",
            export_path="/tmp/selected-work-log.md",
        ),
        created_at="2026-06-19 10:02:00",
    )
    _insert_receipt(
        project,
        receipt_id="unrelated_export",
        action_kind="export.work_log",
        body=_receipt_body(
            op_ids=[4],
            evidence_kind="unrelated",
            output_kind="artifact",
            export_path="/tmp/unrelated-work-log.md",
        ),
        created_at="2026-06-19 10:03:00",
    )
    return project


def test_trace_row_deep_dive_scans_compressed_log_for_latest_record(
    tmp_path: Path,
) -> None:
    run_id = 42
    _write_trace_sidecar(tmp_path, run_id)

    recorded = trace.read_trace_row(tmp_path, run_id, 10)
    assert recorded is not None
    assert recorded["trace_id"] == "trace-index"
    assert recorded["row"]["raw_response"] == "latest"
    assert recorded["record_count"] == 2
    assert "compacted" not in recorded

    missing = trace.read_trace_row(tmp_path, run_id, 12)
    assert missing is not None
    assert missing["row"] is None
    assert missing["record_count"] == 0
    assert not any(tmp_path.rglob("*trace-index*"))


def test_work_log_receipt_summaries_keep_complete_audit_export_from_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _receipt_project(tmp_path)
    receipt_index.build_receipt_index(project)

    def fail_body_parse(_raw: str | None) -> dict[str, Any]:
        raise AssertionError("indexed work-log path must not parse receipt bodies")

    monkeypatch.setattr(receipt_index, "_json_body", fail_body_parse)

    payload = build_work_log_payload(project, "receipt-index")
    receipt_ids = [receipt["id"] for receipt in payload["receipts"]]

    assert receipt_ids == [
        "wf_import_a",
        "wf_import_b",
        "wf_export",
        "unrelated_export",
    ]
    assert payload["receipts"][0]["evidence"] == ["source_file"]
    assert payload["receipts"][2]["outputs"] == ["artifact", "export_artifact"]
    assert payload["receipts"][3]["evidence"] == ["unrelated"]
    assert payload["operations"] == []
