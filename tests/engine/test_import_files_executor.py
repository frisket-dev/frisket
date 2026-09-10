from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore


def _png_header(width: int, height: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I4sII", 13, b"IHDR", width, height)


def _import_files_action(
    file_path: Path | list[Path],
    *,
    sheet_name: str = "Media",
    idempotency_key: str | None = "import_files@sha256:stable",
) -> dict[str, Any]:
    file_paths = file_path if isinstance(file_path, list) else [file_path]
    return {
        "action_id": "import.files",
        "scope": {"kind": "project"},
        "sheet_name": sheet_name,
        "params": {
            "files": [
                {
                    "path": str(path),
                    "filename": path.name,
                    "mime": "image/png",
                }
                for path in file_paths
            ],
        },
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del project
    image_path = tmp_path / "card.png"
    image_path.write_bytes(_png_header(11, 7))
    return {"dir": tmp_path, "path": image_path}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_files_action(seeded["path"])


def _missing_idempotency_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_files_action(seeded["path"], idempotency_key=None)


def _missing_file_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _import_files_action(
        seeded["dir"] / "missing.png",
        idempotency_key="import_files@sha256:missing-file",
    )


def _legacy_envelope_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _import_files_action(seeded["path"])
    return {
        "schema_version": "frisket.action.v2",
        "kind": action["action_id"],
        "capabilities": ["project:write"],
        "params": {**action["params"], "sheet_name": action["sheet_name"]},
        "idempotency_key": action["idempotency_key"],
    }


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == [1]
    assert [output.kind for output in result.outputs] == [
        "sheet",
        "column",
        "column",
        "column",
        "rows",
    ]

    sheet = project.db.execute("SELECT * FROM sheets WHERE name='Media'").fetchone()
    assert sheet is not None
    assert project.row_count(int(sheet["id"])) == 1
    columns = {
        row["name"]: row
        for row in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=? ORDER BY position",
            (sheet["id"],),
        ).fetchall()
    }
    assert columns["filename"]["type"] == "text"
    assert columns["media"]["type"] == "image"
    assert columns["size"]["type"] == "integer"
    assert columns["size"]["format"] == "filesize"
    media_values = project.get_values(int(sheet["id"]), int(columns["media"]["id"]))
    media_cell = next(iter(media_values.values()))
    assert media_cell["mime"] == "image/png"
    assert media_cell["filename"] == "card.png"
    assert set(media_cell) == {"blob", "mime", "filename"}
    blob = project.db.execute(
        "SELECT * FROM blobs WHERE hash=?", (media_cell["blob"],)
    ).fetchone()
    assert blob is not None
    assert blob["filename"] == "card.png"
    assert blob["mime"] == "image/png"
    assert int(blob["size"]) == len(_png_header(11, 7))
    assert MediaBlobStore(project).probe_metadata(media_cell["blob"]) == {
        "kind": "image",
        "size_bytes": len(_png_header(11, 7)),
        "format": "png",
        "width": 11,
        "height": 7,
    }

    op = project.db.execute("SELECT * FROM ops WHERE id=1").fetchone()
    assert op is not None
    assert op["kind"] == "import.files"
    op_spec = json.loads(op["spec"])
    assert op_spec["action_id"] == "import.files"
    assert op_spec["scope"] == {"kind": "project"}
    assert op_spec["sheet_name"] == "Media"
    assert op_spec["import_row_count"] == 1
    assert "rows" not in op_spec["params"]

    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    assert receipt_row is not None
    assert receipt_row["action_kind"] == "import.files"
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.idempotency_key == "import_files@sha256:stable"
    assert receipt.op_ids == [1]
    file_reads = [
        item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
    ]
    assert file_reads == [
        {
            "kind": "local_file_read",
            "path": str(seeded["path"]),
            "sha256": "sha256:" + hashlib.sha256(_png_header(11, 7)).hexdigest(),
            "byte_count": len(_png_header(11, 7)),
        }
    ]
    evidence_refs = [item.ref for item in receipt.evidence]
    assert {item["kind"] for item in evidence_refs} >= {
        "import_source",
        "source_rows",
        "source_cell",
        "imported_blob",
    }
    blob_refs = [item for item in evidence_refs if item["kind"] == "imported_blob"]
    assert len(blob_refs) == 1
    assert blob_refs[0] == {
        "kind": "imported_blob",
        "hash": media_cell["blob"],
        "filename": "card.png",
        "mime": "image/png",
        "size": len(_png_header(11, 7)),
        "row_index": 1,
        "sheet_id": int(sheet["id"]),
        "row_id": next(iter(media_values)),
        "column_id": int(columns["media"]["id"]),
        "op_id": 1,
    }


CASES = [
    ExecutorCase(
        kind="import.files",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write",),
            side_effects=frozenset(
                {
                    "read_local_file",
                    "write_blob_store",
                    "write_evidence_links",
                    "create_sheet",
                    "create_columns",
                    "create_rows",
                    "write_op",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_file_source",
                    "idempotency_conflict",
                }
            ),
            input_schema_properties=("files",),
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
            Gate("invalid_file_source", _missing_file_action, "invalid_file_source"),
            Gate(
                "legacy_envelope",
                _legacy_envelope_action,
                "invalid_action_request",
            ),
        ),
        expect_counts={
            "sheets": 1,
            "columns": 3,
            "rows": 1,
            "ops": 1,
            "receipts": 1,
            "blobs": 1,
        },
        check_state=_check_state,
        request_style="typed",
    )
]


def test_import_files_replay_survives_source_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay reconstructs the result from the stored receipt + blob store, so
    it succeeds with stable ids even after the imported file is gone."""
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


@pytest.mark.parametrize("damage", ["hidden_sheet", "metadata", "missing", "corrupt"])
def test_import_files_replay_refuses_damaged_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        sheet_id = first.outputs[0].sheet_id
        column_id = first.outputs[0].ref["columns"]["media"]
        digest = next(iter(env.project.get_values(sheet_id, column_id).values()))[
            "blob"
        ]
        if damage == "hidden_sheet":
            env.project.db.execute("UPDATE sheets SET hidden=1 WHERE id=?", (sheet_id,))
        elif damage == "metadata":
            env.project.db.execute("DELETE FROM blobs WHERE hash=?", (digest,))
        else:
            with env.project.materialize_blob(digest) as path:
                if damage == "missing":
                    path.unlink()
                else:
                    path.write_bytes(b"corrupt published bytes")
        env.project.db.commit()
        before = env.counts()
        env.seeded["path"].unlink()
        replay = env.run_primary()
        assert replay.status == "failed"
        assert replay.errors[0].code == "stale_replay"
        assert env.counts() == before


def test_import_files_replay_does_not_reread_changed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typed retries recover the original publication, not current source bytes."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        before = env.counts()

        env.seeded["path"].write_bytes(_png_header(12, 8))
        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        _check_state(env.project, env.seeded, replay)
        assert env.counts() == before


def test_import_files_multi_file_import_replays_original_order_and_cells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted and changed inputs cannot alter a completed multi-file import."""
    from frisket.engine.executor import table_action

    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first_path = tmp_path / "first.png"
        second_path = tmp_path / "second.png"
        first_path.write_bytes(_png_header(3, 5))
        second_path.write_bytes(_png_header(5, 8))
        action = _import_files_action(
            [first_path, second_path],
            sheet_name="Multi Media",
            idempotency_key="import_files@sha256:multi",
        )
        first = env.run(action)
        assert first.status == "completed", first.errors
        before = env.counts()
        sheet_id = first.outputs[0].sheet_id
        columns = first.outputs[0].ref["columns"]
        original = env.project.get_values(sheet_id, columns["media"])
        assert [cell["filename"] for cell in original.values()] == [
            "first.png",
            "second.png",
        ]

        first_path.unlink()
        second_path.write_bytes(_png_header(13, 21))
        replay = env.run(action)
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert replay.op_ids == first.op_ids
        assert env.project.get_values(sheet_id, columns["media"]) == original
        assert env.counts() == before


def test_import_files_late_source_failure_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flushed lazy batch remains hidden and is removed on later failure."""
    from frisket.engine.executor import table_action
    from frisket.engine.executor.local_file_read import AdmittedLocalFileReader

    monkeypatch.setattr(table_action, "_TABLE_BATCH_SIZE", 1)
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first_path = tmp_path / "first.png"
        first_path.write_bytes(_png_header(3, 5))
        missing_path = tmp_path / "missing.png"
        original_open = AdmittedLocalFileReader.open_binary

        def observe_before_late_open(reader, path):
            if path == str(missing_path):
                assert (
                    env.project.db.execute(
                        "SELECT count(*) FROM sheets WHERE hidden=0"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    env.project.db.execute("SELECT count(*) FROM ops").fetchone()[0]
                    == 0
                )
                assert (
                    env.project.db.execute("SELECT count(*) FROM receipts").fetchone()[
                        0
                    ]
                    == 0
                )
                assert (
                    env.project.db.execute("SELECT count(*) FROM blobs").fetchone()[0]
                    == 0
                )
            return original_open(reader, path)

        monkeypatch.setattr(
            AdmittedLocalFileReader, "open_binary", observe_before_late_open
        )
        result = env.run(
            _import_files_action(
                [first_path, missing_path],
                sheet_name="Late failure",
                idempotency_key="import_files@sha256:late-failure",
            )
        )

        assert result.status == "failed"
        assert result.errors[0].code == "invalid_file_source"
        assert env.project.db.execute("SELECT count(*) FROM sheets").fetchone()[0] == 0
        assert env.project.db.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0
        assert env.project.db.execute("SELECT count(*) FROM ops").fetchone()[0] == 0
        assert (
            env.project.db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 0
        )
