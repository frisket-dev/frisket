from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project

ROWS = [
    {
        "name": "Civic Hall",
        "homepage": "https://example.com/civic",
        "score": 0.93,
        "location": {"lat": 40.7128, "lon": -74.006},
        "payload": {"rank": 1, "tags": ["city", "contract"]},
    },
    {
        "name": "Library",
        "homepage": "https://example.com/library",
        "score": 0.51,
        "location": {"lat": 41.0, "lon": -73.5},
        "payload": {"rank": 2, "tags": ["public"]},
    },
]


def _write_ndjson(dir_path: Path, name: str, rows: list[dict]) -> Path:
    path = dir_path / name
    path.write_text(
        "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    return path


def _import_ndjson_action(
    source_path: Path,
    *,
    sheet_name: str = "Places",
    idempotency_key: str | None = "import_ndjson@sha256:stable",
) -> dict[str, Any]:
    return {
        "action_id": "import.ndjson",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "source": {
                "kind": "file",
                "path": str(source_path),
                "label": "places.ndjson",
            },
            "encoding": "utf-8",
            "columns": [
                {"name": "name", "type": "text"},
                {"name": "homepage", "type": "url"},
                {"name": "score", "type": "number"},
                {"name": "location", "type": "geo_point"},
                {"name": "payload", "type": "json"},
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project
    return {"dir": tmp_path, "path": _write_ndjson(tmp_path, "places.ndjson", ROWS)}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_ndjson_action(seeded["path"])


def _missing_idempotency_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _import_ndjson_action(seeded["path"], idempotency_key=None)
    action.pop("idempotency_key")
    return action


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_ndjson_action(
        seeded["dir"] / "missing.ndjson",
        idempotency_key="import_ndjson@sha256:missing-source",
    )


def _malformed_action(seeded: dict[str, Any]) -> dict[str, Any]:
    path = seeded["dir"] / "malformed.ndjson"
    path.write_text(
        json.dumps(ROWS[0], sort_keys=True) + "\n{not-json}\n", encoding="utf-8"
    )
    return _import_ndjson_action(
        path, sheet_name="Malformed", idempotency_key="import_ndjson@sha256:malformed"
    )


def _invalid_value_action(seeded: dict[str, Any]) -> dict[str, Any]:
    path = _write_ndjson(
        seeded["dir"], "invalid-value.ndjson", [{**ROWS[0], "score": "high"}]
    )
    return _import_ndjson_action(
        path, sheet_name="Bad Values", idempotency_key="import_ndjson@sha256:invalid"
    )


def _row_shape_action(seeded: dict[str, Any]) -> dict[str, Any]:
    short = {key: value for key, value in ROWS[0].items() if key != "payload"}
    path = _write_ndjson(seeded["dir"], "row-shape.ndjson", [short])
    return _import_ndjson_action(
        path, sheet_name="Bad Shape", idempotency_key="import_ndjson@sha256:row-shape"
    )


def _conflict_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary import, different sheet name.
    return _import_ndjson_action(seeded["path"], sheet_name="Other Places")


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == [1]
    assert [output.kind for output in result.outputs] == [
        "sheet",
        *(["column"] * 5),
        "rows",
    ]

    sheet = project.db.execute("SELECT * FROM sheets WHERE name='Places'").fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 2
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet["id"],),
        ).fetchall()
    }
    assert columns["homepage"]["type"] == "link"
    assert columns["score"]["type"] == "number"
    assert columns["location"]["type"] == "geo_point"
    assert columns["payload"]["type"] == "json"
    scores = project.get_values(int(sheet["id"]), int(columns["score"]["id"]))
    assert list(scores.values()) == [0.93, 0.51]
    locations = project.get_values(int(sheet["id"]), int(columns["location"]["id"]))
    assert list(locations.values())[0] == {"lat": 40.7128, "lon": -74.006}
    payloads = project.get_values(int(sheet["id"]), int(columns["payload"]["id"]))
    assert list(payloads.values())[1] == {"rank": 2, "tags": ["public"]}

    op = project.db.execute("SELECT * FROM ops WHERE id=1").fetchone()
    assert op is not None
    assert op["kind"] == "import.ndjson"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "import.ndjson"
    assert op_spec["scope"] == {"kind": "project"}
    assert op_spec["sheet_name"] == "Places"
    assert op_spec["params"]["source"]["kind"] == "file"
    assert op_spec["import_row_count"] == 2
    assert "rows" not in op_spec["params"]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["action_kind"] == "import.ndjson"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.schema_version == "frisket.receipt.v1"
    assert receipt.idempotency_key == "import_ndjson@sha256:stable"
    assert receipt.op_ids == [1]
    file_read = next(
        item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
    )
    raw = seeded["path"].read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    assert file_read == {
        "kind": "local_file_read",
        "path": str(seeded["path"]),
        "sha256": digest,
        "byte_count": len(raw),
    }
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "import_source",
        "source_rows",
        "source_cell",
    }
    source_refs = [
        item.ref for item in receipt.evidence if item.ref["kind"] == "import_source"
    ]
    assert source_refs[0]["source_kind"] == "ndjson"
    assert source_refs[0]["importer"] == "ndjson"
    assert source_refs[0]["path"] == str(seeded["path"])
    assert source_refs[0]["fingerprint"] == digest
    assert source_refs[0]["request_hash"] == receipt.params_hash
    assert source_refs[0]["line_count"] == 2


CASES = [
    ExecutorCase(
        kind="import.ndjson",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_local_file",
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_file_source",
                    "row_shape_mismatch",
                    "invalid_action_request",
                    "idempotency_conflict",
                }
            ),
            input_schema_properties=("source", "columns"),
            output_schema_properties=(),
        ),
        seed=_seed,
        make_action=_make_action,
        gates=(
            Gate(
                "missing_idempotency_key",
                _missing_idempotency_action,
                "invalid_action_request",
            ),
            Gate(
                "invalid_file_source",
                _missing_source_action,
                "invalid_file_source",
            ),
            Gate("ndjson_parse_failed", _malformed_action, "ndjson_parse_failed"),
            Gate("invalid_ndjson_value", _invalid_value_action, "invalid_ndjson_value"),
            Gate("row_shape_mismatch", _row_shape_action, "row_shape_mismatch"),
            Gate(
                "idempotency_conflict",
                _conflict_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={"sheets": 1, "columns": 5, "rows": 2, "ops": 1, "receipts": 1},
        check_state=_check_state,
        request_style="typed",
    )
]


def test_import_ndjson_replay_survives_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay reconstructs the result from the stored receipt, so it succeeds
    (with stable ids and no new writes) even after the source file is gone."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        before = env.counts()

        env.seeded["path"].unlink()
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert env.counts() == before
