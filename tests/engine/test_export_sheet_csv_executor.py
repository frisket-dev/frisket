from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project

_COUNT_TABLES = (
    "sheets",
    "columns",
    "rows",
    "runs",
    "results",
    "ops",
    "edits",
    "receipts",
)


def _run_seed_action(project: Project, action: dict[str, Any]) -> Any:
    from frisket.engine.executor import run_action_spec

    result = run_action_spec(project, action, project_id="project-sheet-csv")
    assert result.status == "completed", result.errors
    return result


def _seed_import_action() -> dict[str, Any]:
    return {
        "action_id": "import.rows",
        "scope": {"kind": "project"},
        "sheet_name": "Stories",
        "params": {
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "note", "type": "text"},
                {"name": "count", "type": "integer"},
                {"name": "metadata", "type": "json"},
            ],
            "rows": [
                {
                    "name": "Ada",
                    "note": "hello, world",
                    "count": 2,
                    "metadata": {"b": 2, "a": 1},
                },
                {
                    "name": "Grace",
                    "note": "plain",
                    "count": 3,
                    "metadata": {"source": "archive", "ok": True},
                },
            ],
            "source": {
                "kind": "inline",
                "label": "sheet csv seed",
                "fingerprint": "sha256:sheet-csv-seed",
            },
        },
        "idempotency_key": "sheet_csv_seed@sha256:v1",
    }


def _map_risk_action(sheet_id: int) -> dict[str, Any]:
    code = "\n".join(
        [
            "risk = 'high' if row['count'] >= 3 else 'low'",
            "result = {'risk': risk}",
        ]
    )
    return {
        "action_id": "map.python",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "input_columns": ["name", "note", "count", "metadata"],
            "code": code,
            "return_schema": {
                "type": "object",
                "required": ["risk"],
                "properties": {"risk": {"type": "string"}},
            },
            "output_routes": [
                {
                    "name": "risk",
                    "path": "$.risk",
                    "target": {
                        "kind": "column",
                        "type": "text",
                    },
                }
            ],
        },
        "idempotency_key": "sheet_csv_map@sha256:v1",
    }


def _review_edit_action(
    *,
    run_id: int,
    row_id: int,
    column_id: int,
    value: str,
) -> dict[str, Any]:
    return {
        "action_id": "review.decision",
        "scope": {"kind": "project"},
        "params": {
            "run_id": run_id,
            "row_id": row_id,
            "column_id": column_id,
            "decision": "edit",
            "value": value,
        },
        "idempotency_key": "sheet_csv_review_edit@sha256:v1",
    }


def _export_action(
    *,
    sheet_id: int,
    path: Path,
    key: str = "sheet_csv_export@sha256:v1",
) -> dict[str, Any]:
    return {
        "action_id": "export.sheet_csv",
        "scope": {"kind": "project"},
        "params": {
            "sheet_id": sheet_id,
            "destination": {"kind": "local_file", "path": str(path)},
        },
        "idempotency_key": key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    """A sheet whose live values mix source cells, a map run, and a later edit.

    The export must serialize what the user currently sees.
    """
    _run_seed_action(project, _seed_import_action())
    sheet_id = int(
        project.db.execute("SELECT id FROM sheets WHERE name='Stories'").fetchone()[
            "id"
        ]
    )
    first_row_id = int(
        project.db.execute(
            "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
            (sheet_id,),
        ).fetchone()["id"]
    )
    mapped = _run_seed_action(project, _map_risk_action(sheet_id))
    risk_column_id = int(
        project.db.execute(
            "SELECT id FROM columns WHERE sheet_id=? AND name='risk'",
            (sheet_id,),
        ).fetchone()["id"]
    )
    _run_seed_action(
        project,
        _review_edit_action(
            run_id=mapped.run_id,
            row_id=first_row_id,
            column_id=risk_column_id,
            value="medium, verified",
        ),
    )
    out_dir = tmp_path / "artifacts"
    out_dir.mkdir()
    return {
        "dir": tmp_path,
        "sheet_id": sheet_id,
        "out": out_dir / "stories.csv",
    }


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(sheet_id=seeded["sheet_id"], path=seeded["out"])


def _invalid_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        sheet_id=999_999,
        path=seeded["dir"] / "missing-sheet.csv",
        key="sheet_csv_missing_sheet@sha256:v1",
    )


def _invalid_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        sheet_id=seeded["sheet_id"],
        path=seeded["dir"] / "missing-parent" / "stories.csv",
        key="sheet_csv_invalid_destination@sha256:v1",
    )


def _conflicting_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary export, different destination.
    return _export_action(sheet_id=seeded["sheet_id"], path=seeded["dir"] / "other.csv")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == []
    output_path: Path = seeded["out"]
    export_output = next(output for output in result.outputs if output.kind == "export")
    assert export_output.name == "sheet_csv"
    assert export_output.ref["kind"] == "export_artifact"
    assert export_output.ref["format"] == "csv"
    assert export_output.ref["sheet_id"] == seeded["sheet_id"]
    assert export_output.ref["path"] == str(output_path)
    assert export_output.ref["byte_count"] == output_path.stat().st_size
    assert export_output.ref["sha256"].startswith("sha256:")

    # Live values: the later edit wins over the map value, JSON cells
    # serialize with sorted keys, and quoting survives embedded commas.
    rows = list(csv.DictReader(io.StringIO(output_path.read_text(encoding="utf-8"))))
    assert rows == [
        {
            "name": "Ada",
            "note": "hello, world",
            "count": "2",
            "metadata": '{"a": 1, "b": 2}',
            "risk": "medium, verified",
        },
        {
            "name": "Grace",
            "note": "plain",
            "count": "3",
            "metadata": '{"ok": true, "source": "archive"}',
            "risk": "high",
        },
    ]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "export.sheet_csv"
    assert receipt.outputs[0].ref["kind"] == "export_artifact"
    assert receipt.outputs[0].ref["sheet_id"] == seeded["sheet_id"]
    assert receipt.exports[0]["path"] == str(output_path)
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "exported_sheet",
        "exported_columns",
        "exported_rows",
    }
    assert receipt.evidence[0].retention == "pinned"


CASES = [
    ExecutorCase(
        kind="export.sheet_csv",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:read", "project:write"),
            side_effects=frozenset(
                {
                    "read_sheet_values",
                    "write_export_artifact",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_export_destination",
                    "invalid_sheet_ref",
                    "invalid_query_spec",
                    "invalid_query_filter",
                    "export_rowset_too_large",
                    "export_artifact_missing",
                    "export_artifact_mismatch",
                    "idempotency_conflict",
                    "project_write_failed",
                }
            ),
            cost_policy_kind="none",
        ),
        seed=_seed,
        make_action=_make_action,
        request_style="typed",
        gates=(
            Gate(
                "invalid_sheet_ref",
                _invalid_sheet_action,
                "invalid_sheet_ref",
                no_writes=False,  # The callable host records the failed invocation.
            ),
            Gate(
                "invalid_export_destination",
                _invalid_destination_action,
                "invalid_export_destination",
                no_writes=False,
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_destination_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={table: 0 for table in _COUNT_TABLES} | {"receipts": 1},
        check_state=_check_state,
    )
]


def test_export_sheet_csv_artifact_trust_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay re-verifies the artifact bytes, and a failed export never leaves
    a file behind."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        sha = next(o for o in first.outputs if o.kind == "export").ref["sha256"]
        after = env.counts()

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert (
            next(o for o in replay.outputs if o.kind == "export").ref["sha256"] == sha
        )

        invalid_sheet = env.run(_invalid_sheet_action(env.seeded))
        assert invalid_sheet.status == "failed"
        assert invalid_sheet.errors[0].code == "invalid_sheet_ref"
        assert not (env.seeded["dir"] / "missing-sheet.csv").exists()
        assert invalid_sheet.receipt_id is not None
        after = env.counts()

        output_path: Path = env.seeded["out"]
        output_path.write_text("tampered\n", encoding="utf-8")
        tampered = env.run_primary()
        assert tampered.status == "failed"
        assert tampered.errors[0].code == "export_artifact_mismatch"
        assert env.counts() == after

        output_path.unlink()
        missing = env.run_primary()
        assert missing.status == "failed"
        assert missing.errors[0].code == "export_artifact_missing"
        assert env.counts() == after


def test_export_sheet_csv_replay_hashes_artifact_in_bounded_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay verification must not rehydrate a large artifact with read_bytes."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        output_path: Path = env.seeded["out"]
        original_open = Path.open
        reads: list[int] = []

        class ObservedReader:
            def __init__(self, handle: Any):
                self._handle = handle

            def read(self, size: int = -1) -> bytes:
                reads.append(size)
                assert 0 <= size <= 64 * 1024
                return self._handle.read(size)

            def __enter__(self):
                self._handle.__enter__()
                return self

            def __exit__(self, *args: Any):
                return self._handle.__exit__(*args)

            def __getattr__(self, name: str) -> Any:
                return getattr(self._handle, name)

        def observed_open(path: Path, *args: Any, **kwargs: Any):
            mode = kwargs.get("mode", args[0] if args else "r")
            handle = original_open(path, *args, **kwargs)
            if path == output_path and mode == "rb":
                return ObservedReader(handle)
            return handle

        monkeypatch.setattr(
            Path,
            "read_bytes",
            lambda _path: (_ for _ in ()).throw(AssertionError("no eager replay read")),
        )
        monkeypatch.setattr(Path, "open", observed_open)
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert reads and max(reads) <= 64 * 1024
