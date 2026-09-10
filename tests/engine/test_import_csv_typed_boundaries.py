from __future__ import annotations

import copy
import hashlib
import json
from contextlib import closing
from pathlib import Path

import pytest

from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.streaming_import import StreamingSheetWriter
from tests.engine.test_import_csv_executor import _import_csv_action


def _counts(project: Project) -> dict[str, int]:
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("sheets", "columns", "rows", "cells", "ops", "receipts")
    }


def _action(path: Path) -> dict:
    action = _import_csv_action(path)
    action["params"]["columns"] = [{"name": "value", "type": "integer"}]
    return action


def test_csv_completed_replay_does_not_reopen_deleted_source(tmp_path: Path) -> None:
    path = tmp_path / "source.csv"
    path.write_bytes(b"value\r\n1\r\n2\r\n")
    with closing(Project.create(tmp_path / "replay.frisket", name="Replay")) as project:
        action = _action(path)
        first = run_action_spec(project, action, project_id="p")
        assert first.status == "completed", first.errors
        before = _counts(project)
        path.unlink()
        replay = run_action_spec(project, action, project_id="p")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert replay.outputs == first.outputs
        assert _counts(project) == before


@pytest.mark.parametrize("changed", ["sheet_name", "source", "encoding", "output_name"])
def test_csv_same_key_rejects_changed_request_without_source_access(
    tmp_path: Path, changed: str
) -> None:
    path = tmp_path / "source.csv"
    path.write_text("value\n1\n")
    with closing(
        Project.create(tmp_path / "conflict.frisket", name="Conflict")
    ) as project:
        action = _action(path)
        first = run_action_spec(project, action, project_id="p")
        assert first.status == "completed", first.errors
        before = _counts(project)
        path.unlink()
        changed_action = copy.deepcopy(action)
        if changed == "sheet_name":
            changed_action["sheet_name"] = "Other sheet"
        elif changed == "source":
            changed_action["params"]["sources"][0]["path"] = str(tmp_path / "other.csv")
        elif changed == "encoding":
            changed_action["params"]["sources"][0]["encoding"] = "cp1252"
        else:
            changed_action["output_names"] = {"value": "Other column"}
        result = run_action_spec(project, changed_action, project_id="p")
        assert result.status == "failed"
        assert result.errors[0].code == "idempotency_conflict"
        assert _counts(project) == before


@pytest.mark.parametrize("max_rows", [1, 2])
def test_csv_finite_row_budget_never_leaves_partial_sheet(
    tmp_path: Path, max_rows: int
) -> None:
    path = tmp_path / "budget.csv"
    path.write_text("value\n1\n2\n")
    with closing(Project.create(tmp_path / "budget.frisket", name="Budget")) as project:
        before = _counts(project)
        result = run_action_spec(
            project,
            _action(path),
            project_id="p",
            deps=ExecutorDeps(
                import_workload_limits=ImportWorkloadLimits(max_rows=max_rows)
            ),
        )
        if max_rows == 1:
            assert result.status == "failed"
            assert result.errors[0].code == "import_workload_limit_exceeded"
            assert _counts(project) == before
        else:
            assert result.status == "completed", result.errors
            assert project.row_count(result.outputs[0].sheet_id) == 2


def test_csv_multiple_source_dialects_preserve_schema_values_and_raw_read_facts(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.csv"
    first_bytes = "name,amount\r\nFirst,1.5\r\n".encode("utf-8")
    first_path.write_bytes(first_bytes)
    second_path = tmp_path / "second.csv"
    second_bytes = "amount;name\r\n2,5;Café\r\n;Empty\r\n".encode("cp1252")
    second_path.write_bytes(second_bytes)
    with closing(Project.create(tmp_path / "multi.frisket", name="Multi")) as project:
        action = _import_csv_action(first_path)
        action["params"] = {
            "sources": [
                {"path": str(first_path), "label": "first"},
                {
                    "path": str(second_path),
                    "label": "second",
                    "encoding": "cp1252",
                    "delimiter": ";",
                    "decimal_separator": ",",
                    "headers": ["amount", "name"],
                },
            ],
            "columns": [
                {"name": "name", "type": "text"},
                {
                    "name": "amount",
                    "type": "number",
                    "format": "number",
                    "hidden": True,
                },
                {"name": "origin", "type": "text"},
            ],
            "source_label_column": "origin",
        }
        action["output_names"] = {"amount": "Price", "origin": "Source"}
        result = run_action_spec(project, action, project_id="p")
        assert result.status == "completed", result.errors
        sheet_id = result.outputs[0].sheet_id
        columns = {
            col["name"]: col for col in project.columns(sheet_id, include_hidden=True)
        }
        assert set(columns) == {"name", "Price", "Source"}
        assert columns["Price"]["type"] == "number"
        assert columns["Price"]["format"] == "number"
        assert columns["Price"]["hidden"] == 1
        assert list(project.get_values(sheet_id, columns["name"]["id"]).values()) == [
            "First",
            "Café",
            "Empty",
        ]
        assert list(project.get_values(sheet_id, columns["Price"]["id"]).values()) == [
            1.5,
            2.5,
            None,
        ]
        assert list(project.get_values(sheet_id, columns["Source"]["id"]).values()) == [
            "first",
            "second",
            "second",
        ]
        receipt = json.loads(
            project.db.execute(
                "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
            ).fetchone()[0]
        )
        reads = [
            item["ref"]
            for item in receipt["inputs"]
            if item["ref"]["kind"] == "local_file_read"
        ]
        assert reads == [
            {
                "kind": "local_file_read",
                "path": str(path),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "byte_count": len(raw),
            }
            for path, raw in ((first_path, first_bytes), (second_path, second_bytes))
        ]


def test_csv_late_value_error_aborts_already_committed_hidden_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "late.csv"
    path.write_text("value\n" + "1\n" * 1_001 + "not-an-integer\n")
    with closing(Project.create(tmp_path / "late.frisket", name="Late")) as project:
        before = _counts(project)
        appended_batches = 0
        append = StreamingSheetWriter.append_rows

        def observe_append(writer, rows):
            nonlocal appended_batches
            result = append(writer, rows)
            appended_batches += 1
            assert project.sheets() == []
            assert project.db.execute("SELECT COUNT(*) FROM rows").fetchone()[0] > 0
            return result

        monkeypatch.setattr(StreamingSheetWriter, "append_rows", observe_append)
        result = run_action_spec(project, _action(path), project_id="p")
        assert appended_batches > 0
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_csv_value"
        assert result.errors[0].details["line"] == 1_003
        assert result.errors[0].details["column"] == "value"
        assert _counts(project) == before
