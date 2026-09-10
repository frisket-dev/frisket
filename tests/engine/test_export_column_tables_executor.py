"""export.column_tables: explode a JSON list column into a zip of per-table CSVs.

Shapes pinned: pdf_tables-shaped list-of-dicts (primary case, with group_by +
the exclude_columns metadata-exclusion precedent), list-of-lists, and scalar
lists; the zip + manifest.json provenance sidecar; collision determinism via
frisket.output_names; local_dir and project_file destinations.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from executor_harness import CatalogEntry, ExecutorCase, Gate, case_env
from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project

_COUNT_TABLES = ("sheets", "columns", "rows", "receipts", "blobs")


# --- fixtures --------------------------------------------------------


def _seed_pdf_tables_sheet(project: Project) -> tuple[int, int, int]:
    """A sheet shaped like media.extract_pdf_tables' output: one row per
    source PDF, a JSON "pdf_tables" column whose cell is a FLAT list of
    per-table-row record dicts carrying the 8 pdf_tables metadata keys
    (source_row_id, source_filename, source_blob_hash, page_start, page_end,
    table_index, table_row_index, raw_cells_json) plus the extracted data
    columns -- built directly (not via the real natural_pdf/extract_pdf_tables
    pipeline) so CSV/manifest assertions stay exact and fast.

    Returns (sheet_id, pdf_tables_column_id, title_column_id).
    """
    sheet_id = project.add_sheet("Documents")
    columns = {
        "title": project.add_column(sheet_id, "title", "text"),
        "pdf_tables": project.add_column(sheet_id, "pdf_tables", "json"),
    }

    def record(
        *,
        source_row_id: int,
        source_filename: str,
        table_index: int,
        row_index: int,
        **data: Any,
    ) -> dict[str, Any]:
        rec = {
            "source_row_id": source_row_id,
            "source_filename": source_filename,
            "source_blob_hash": f"sha256:{source_filename}",
            "page_start": 1,
            "page_end": 1,
            "table_index": table_index,
            "table_row_index": row_index,
            "raw_cells_json": list(data.values()),
        }
        rec.update(data)
        return rec

    alpha_items = [
        record(
            source_row_id=1,
            source_filename="alpha.pdf",
            table_index=0,
            row_index=1,
            vendor="Acme",
            amount="10",
        ),
        record(
            source_row_id=1,
            source_filename="alpha.pdf",
            table_index=0,
            row_index=2,
            vendor="Beta",
            amount="12",
        ),
        record(
            source_row_id=1,
            source_filename="alpha.pdf",
            table_index=1,
            row_index=1,
            vendor="Gamma",
            amount="14",
        ),
    ]
    bravo_items = [
        record(
            source_row_id=2,
            source_filename="bravo.pdf",
            table_index=0,
            row_index=1,
            vendor="Delta",
            amount="20",
        ),
    ]

    row_ids = project.add_rows(
        sheet_id,
        [
            {"title": "alpha.pdf", "pdf_tables": alpha_items},
            {"title": "bravo.pdf", "pdf_tables": bravo_items},
            {"title": "empty.pdf", "pdf_tables": []},
        ],
        columns,
    )
    # record() closes over row_ids indirectly via source_row_id above; fix up
    # the actual row ids used for the source_row_id field to match reality.
    for item in alpha_items:
        item["source_row_id"] = row_ids[0]
    for item in bravo_items:
        item["source_row_id"] = row_ids[1]
    project.db.execute(
        "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
        (json.dumps(alpha_items), row_ids[0], columns["pdf_tables"]),
    )
    project.db.execute(
        "UPDATE cells SET value=? WHERE row_id=? AND column_id=?",
        (json.dumps(bravo_items), row_ids[1], columns["pdf_tables"]),
    )
    project.db.commit()
    return sheet_id, columns["pdf_tables"], columns["title"]


def _seed_list_of_lists_sheet(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("Raw tables")
    columns = {"raw": project.add_column(sheet_id, "raw", "json")}
    project.add_rows(
        sheet_id,
        [
            {
                "raw": [
                    ["vendor", "amount"],
                    ["Acme", "10"],
                    ["Beta", "12"],
                ]
            }
        ],
        columns,
    )
    return sheet_id, columns["raw"]


def _seed_scalar_list_sheet(project: Project) -> tuple[int, int]:
    sheet_id = project.add_sheet("Tags")
    columns = {"tags": project.add_column(sheet_id, "tags", "json")}
    project.add_rows(sheet_id, [{"tags": ["red", "green", "blue"]}], columns)
    return sheet_id, columns["tags"]


def _seed_json_cell_sheet(project: Project, name: str, cell: Any) -> tuple[int, int]:
    sheet_id = project.add_sheet(name)
    columns = {"j": project.add_column(sheet_id, "j", "json")}
    project.add_rows(sheet_id, [{"j": cell}], columns)
    return sheet_id, columns["j"]


_METADATA_EXCLUDE_COLUMNS = [
    "source_row_id",
    "source_filename",
    "source_blob_hash",
    "page_start",
    "page_end",
    "table_row_index",
    "raw_cells_json",
]


def _export_action(
    *,
    sheet_id: int,
    column_id: int,
    destination: dict[str, Any],
    key: str,
    group_by: str | None = None,
    exclude_columns: list[str] | None = None,
    name_template: str | None = None,
    first_row_header: bool | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sheet_id": sheet_id,
        "column_id": column_id,
        "destination": destination,
    }
    if group_by is not None:
        params["group_by"] = group_by
    if exclude_columns is not None:
        params["exclude_columns"] = exclude_columns
    if name_template is not None:
        params["name_template"] = name_template
    if first_row_header is not None:
        params["first_row_header"] = first_row_header
    return {
        "action_id": "export.column_tables",
        "scope": {"kind": "project"},
        "params": params,
        "idempotency_key": key,
    }


def _zip_manifest(zip_path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(zip_path) as zf:
        return json.loads(zf.read("manifest.json"))


def _zip_csv(zip_path: Path, filename: str) -> list[dict[str, str]]:
    with zipfile.ZipFile(zip_path) as zf:
        text = zf.read(filename).decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _zip_names(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.namelist()


# --- shared lifecycle: list-of-dicts primary case (group_by + exclusion) ---


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    sheet_id, column_id, title_column_id = _seed_pdf_tables_sheet(project)
    scalar_sheet_id, scalar_column_id = _seed_scalar_list_sheet(project)
    not_list = _seed_json_cell_sheet(project, "Not list", {"not": "a list"})
    empty = _seed_json_cell_sheet(project, "Empty", [])
    mismatch = _seed_json_cell_sheet(project, "Mismatch", [{"a": 1}, "scalar"])
    only_field = _seed_json_cell_sheet(project, "Only field", [{"only": "field"}])
    dest_dir = tmp_path / "artifacts"
    dest_dir.mkdir()
    return {
        "dir": tmp_path,
        "dest_dir": dest_dir,
        "sheet_id": sheet_id,
        "column_id": column_id,
        "title_column_id": title_column_id,
        "scalar": (scalar_sheet_id, scalar_column_id),
        "not_list": not_list,
        "empty": empty,
        "mismatch": mismatch,
        "only_field": only_field,
    }


def _primary_action(
    seeded: dict[str, Any],
    *,
    key: str = "column_tables_export@sha256:v1",
    **kwargs: Any,
) -> dict[str, Any]:
    return _export_action(
        sheet_id=seeded["sheet_id"],
        column_id=seeded["column_id"],
        destination={"kind": "local_dir", "path": str(seeded["dest_dir"])},
        key=key,
        group_by=kwargs.pop("group_by", "table_index"),
        exclude_columns=kwargs.pop("exclude_columns", _METADATA_EXCLUDE_COLUMNS),
        **kwargs,
    )


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _primary_action(seeded)


def _missing_idempotency_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _primary_action(seeded, key="unused")
    action["idempotency_key"] = None
    return action


def _caller_capability_action(seeded: dict[str, Any]) -> dict[str, Any]:
    action = _primary_action(seeded, key="column_tables_caller_capability@sha256:v1")
    action["capabilities"] = ["project:read"]
    return action


def _invalid_sheet_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        sheet_id=999_999,
        column_id=seeded["column_id"],
        destination={"kind": "local_dir", "path": str(seeded["dest_dir"])},
        key="column_tables_bad_sheet@sha256:v1",
    )


def _invalid_column_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # A text column can never be a table source.
    return _export_action(
        sheet_id=seeded["sheet_id"],
        column_id=seeded["title_column_id"],
        destination={"kind": "local_dir", "path": str(seeded["dest_dir"])},
        key="column_tables_bad_column@sha256:v1",
    )


def _json_cell_gate_action(seeded_key: str, idempotency_key: str, **kwargs: Any) -> Any:
    def make(seeded: dict[str, Any]) -> dict[str, Any]:
        sheet_id, column_id = seeded[seeded_key]
        return _export_action(
            sheet_id=sheet_id,
            column_id=column_id,
            destination={"kind": "local_dir", "path": str(seeded["dest_dir"])},
            key=idempotency_key,
            **kwargs,
        )

    return make


def _invalid_group_by_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _primary_action(
        seeded,
        key="column_tables_bad_group_by@sha256:v1",
        group_by="does_not_exist",
    )


def _invalid_exclude_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _primary_action(
        seeded,
        key="column_tables_bad_exclude@sha256:v1",
        exclude_columns=["does_not_exist"],
    )


def _invalid_template_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _primary_action(
        seeded,
        key="column_tables_bad_template@sha256:v1",
        name_template="{nope}.csv",
    )


def _invalid_destination_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _export_action(
        sheet_id=seeded["sheet_id"],
        column_id=seeded["column_id"],
        destination={"kind": "project_file", "prefix": "../escape"},
        key="column_tables_bad_prefix@sha256:v1",
    )


def _conflicting_params_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary export, different grouping.
    return _primary_action(seeded, group_by=None)


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    assert result.op_ids == []
    export_output = next(o for o in result.outputs if o.kind == "export")
    assert export_output.name == "column_tables"
    ref = export_output.ref
    assert ref["kind"] == "export_artifact"
    assert ref["export_kind"] == "column_tables"
    assert ref["format"] == "zip"
    assert ref["shape"] == "dict"
    assert ref["group_by"] == "table_index"
    zip_path = Path(ref["path"])
    assert zip_path.parent == seeded["dest_dir"]
    assert zip_path.exists()
    assert ref["byte_count"] == zip_path.stat().st_size
    assert ref["sha256"].startswith("sha256:")
    # alpha.pdf: table_index 0 (2 rows) + table_index 1 (1 row); bravo.pdf:
    # table_index 0 (1 row). empty.pdf's [] cell contributes nothing.
    assert ref["artifact_count"] == 3
    assert ref["entry_count"] == 4

    names = _zip_names(zip_path)
    assert set(names) == {"000_000.csv", "000_001.csv", "001_000.csv", "manifest.json"}
    assert _zip_csv(zip_path, "000_000.csv") == [
        {"vendor": "Acme", "amount": "10"},
        {"vendor": "Beta", "amount": "12"},
    ]
    assert _zip_csv(zip_path, "000_001.csv") == [{"vendor": "Gamma", "amount": "14"}]
    assert _zip_csv(zip_path, "001_000.csv") == [{"vendor": "Delta", "amount": "20"}]

    # The metadata keys (group_by + exclude_columns) never leak into a body.
    with zipfile.ZipFile(zip_path) as zf:
        header_row = next(csv.reader(io.StringIO(zf.read("000_000.csv").decode())))
    assert header_row == ["vendor", "amount"]

    manifest = _zip_manifest(zip_path)
    assert manifest["kind"] == "column_tables_export_manifest"
    assert manifest["sheet_id"] == seeded["sheet_id"]
    assert manifest["column_id"] == seeded["column_id"]
    assert manifest["column_name"] == "pdf_tables"
    assert manifest["shape"] == "dict"
    assert manifest["group_by"] == "table_index"
    assert manifest["artifact_count"] == 3
    assert manifest["entry_count"] == 4
    by_filename = {a["filename"]: a for a in manifest["artifacts"]}
    assert by_filename["000_000.csv"]["source_filename"] == "alpha.pdf"
    assert by_filename["000_000.csv"]["source_blob_hash"] == "sha256:alpha.pdf"
    assert by_filename["000_000.csv"]["group_key"] == "table_index"
    assert by_filename["000_000.csv"]["group_value"] == 0
    assert by_filename["000_000.csv"]["entry_count"] == 2
    assert by_filename["000_000.csv"]["columns"] == ["vendor", "amount"]
    assert by_filename["001_000.csv"]["source_filename"] == "bravo.pdf"

    # Receipt: exports carries the same ref, evidence pins the manifest.
    receipt_row = project.db.execute(
        "SELECT * FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.action_kind == "export.column_tables"
    assert receipt.exports[0]["sha256"] == ref["sha256"]
    assert {item.ref["kind"] for item in receipt.evidence} >= {
        "column_tables_export_manifest",
        "column_tables_export_summary",
    }
    assert receipt.evidence[0].retention == "pinned"


CASES = [
    ExecutorCase(
        kind="export.column_tables",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="whole_project",
            async_mode="sync",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:read", "project:write"),
            side_effects=frozenset(
                {
                    "read_sheet_column_values",
                    "write_export_artifact",
                    "write_receipt",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_action_request",
                    "invalid_sheet_ref",
                    "invalid_column_ref",
                    "invalid_group_by",
                    "invalid_exclude_columns",
                    "invalid_name_template",
                    "source_not_list",
                    "empty_source_column",
                    "column_shape_mismatch",
                    "empty_output_columns",
                    "invalid_export_destination",
                    "export_artifact_missing",
                    "export_artifact_mismatch",
                    "idempotency_conflict",
                }
            ),
            cost_policy_kind="none",
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
                "caller_capability_claim",
                _caller_capability_action,
                "invalid_action_request",
            ),
            Gate("invalid_sheet_ref", _invalid_sheet_action, "invalid_sheet_ref"),
            Gate("invalid_column_ref", _invalid_column_action, "invalid_column_ref"),
            Gate(
                "source_not_list",
                _json_cell_gate_action("not_list", "column_tables_not_list@sha256:v1"),
                "source_not_list",
            ),
            Gate(
                "empty_source_column",
                _json_cell_gate_action("empty", "column_tables_empty@sha256:v1"),
                "empty_source_column",
            ),
            Gate(
                "column_shape_mismatch",
                _json_cell_gate_action("mismatch", "column_tables_mismatch@sha256:v1"),
                "column_shape_mismatch",
            ),
            Gate(
                "invalid_group_by_unknown_key",
                _invalid_group_by_action,
                "invalid_group_by",
            ),
            Gate(
                "invalid_group_by_on_scalar_shape",
                _json_cell_gate_action(
                    "scalar",
                    "column_tables_scalar_group_by@sha256:v1",
                    group_by="whatever",
                ),
                "invalid_group_by",
            ),
            Gate(
                "invalid_exclude_columns",
                _invalid_exclude_action,
                "invalid_exclude_columns",
            ),
            Gate(
                "empty_output_columns",
                _json_cell_gate_action(
                    "only_field",
                    "column_tables_empty_cols@sha256:v1",
                    exclude_columns=["only"],
                ),
                "empty_output_columns",
            ),
            Gate(
                "invalid_name_template",
                _invalid_template_action,
                "invalid_name_template",
            ),
            Gate(
                "invalid_export_destination",
                _invalid_destination_action,
                "invalid_export_destination",
            ),
            Gate(
                "idempotency_conflict",
                _conflicting_params_action,
                "idempotency_conflict",
                after_primary_run=True,
            ),
        ),
        expect_counts={table: 0 for table in _COUNT_TABLES} | {"receipts": 1},
        check_state=_check_state,
    )
]


def test_catalog_entry_has_no_project_export_target() -> None:
    """Column-scoped launcher, not a project.export destination surface."""
    from frisket.actions.system import root_action_catalog

    catalog = root_action_catalog()
    entry = next(
        item for item in catalog.actions if item.kind == "export.column_tables"
    )
    assert "export_target" not in entry.ui_hints


def test_artifact_trust_on_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay reuses the same receipt and bytes (no re-render), then detects
    tampered and missing artifacts."""
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        ref = next(o for o in first.outputs if o.kind == "export").ref
        zip_path = Path(ref["path"])
        after = env.counts()

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert (
            next(o for o in replay.outputs if o.kind == "export").ref["sha256"]
            == (ref["sha256"])
        )
        assert env.counts() == after

        zip_path.write_bytes(b"tampered")
        tampered = env.run_primary()
        assert tampered.status == "failed"
        assert tampered.errors[0].code == "export_artifact_mismatch"
        assert env.counts() == after

        zip_path.unlink()
        missing = env.run_primary()
        assert missing.status == "failed"
        assert missing.errors[0].code == "export_artifact_missing"
        assert env.counts() == after


# --- per-shape rendering (unique; the lifecycle above is shape-agnostic) ---


def test_dict_shape_no_group_by_is_one_artifact_per_row(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "no-group.frisket", name="No Group")
    sheet_id, column_id, _title = _seed_pdf_tables_sheet(project)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "local_dir", "path": str(dest_dir)},
        key="no_group@sha256:v1",
        exclude_columns=_METADATA_EXCLUDE_COLUMNS + ["table_index"],
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    ref = next(o for o in result.outputs if o.kind == "export").ref
    zip_path = Path(ref["path"])
    assert set(_zip_names(zip_path)) == {"000_000.csv", "001_000.csv", "manifest.json"}
    alpha = _zip_csv(zip_path, "000_000.csv")
    assert alpha == [
        {"vendor": "Acme", "amount": "10"},
        {"vendor": "Beta", "amount": "12"},
        {"vendor": "Gamma", "amount": "14"},
    ]
    project.close()


def test_list_shape_headerless_default(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "list-shape.frisket", name="List Shape")
    sheet_id, column_id = _seed_list_of_lists_sheet(project)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "local_dir", "path": str(dest_dir)},
        key="list_shape_headerless@sha256:v1",
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    ref = next(o for o in result.outputs if o.kind == "export").ref
    assert ref["shape"] == "list"
    rows = _zip_csv(Path(ref["path"]), "000_000.csv")
    assert rows == [
        {"column_1": "vendor", "column_2": "amount"},
        {"column_1": "Acme", "column_2": "10"},
        {"column_1": "Beta", "column_2": "12"},
    ]
    project.close()


def test_list_shape_first_row_header(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "list-header.frisket", name="List Header")
    sheet_id, column_id = _seed_list_of_lists_sheet(project)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "local_dir", "path": str(dest_dir)},
        key="list_shape_header@sha256:v1",
        first_row_header=True,
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    zip_path = Path(next(o for o in result.outputs if o.kind == "export").ref["path"])
    rows = _zip_csv(zip_path, "000_000.csv")
    assert rows == [
        {"vendor": "Acme", "amount": "10"},
        {"vendor": "Beta", "amount": "12"},
    ]
    project.close()


def test_list_shape_group_by_is_structurally_valid() -> None:
    from frisket.actions.system import validate_root_action

    action = _export_action(
        sheet_id=1,
        column_id=1,
        destination={"kind": "local_dir", "path": "/tmp/whatever"},
        key="k@sha256:v1",
        group_by="whatever",
    )
    validation = validate_root_action(action)
    # group_by is structurally valid; the DB-dependent shape check runs later
    # (the invalid_group_by_on_scalar_shape gate above).
    assert validation.ok is True


def test_scalar_shape_single_value_column(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "scalar-shape.frisket", name="Scalar Shape")
    sheet_id, column_id = _seed_scalar_list_sheet(project)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "local_dir", "path": str(dest_dir)},
        key="scalar_shape@sha256:v1",
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    ref = next(o for o in result.outputs if o.kind == "export").ref
    assert ref["shape"] == "scalar"
    rows = _zip_csv(Path(ref["path"]), "000_000.csv")
    assert rows == [{"value": "red"}, {"value": "green"}, {"value": "blue"}]
    project.close()


def test_name_template_collision_gets_deterministic_suffix(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "collision.frisket", name="Collision")
    sheet_id, column_id, _title = _seed_pdf_tables_sheet(project)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    # Every artifact maps to the SAME filename ("fixed.csv") regardless of
    # row/table -- the util must dedupe deterministically, never overwrite.
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "local_dir", "path": str(dest_dir)},
        key="collision@sha256:v1",
        group_by="table_index",
        exclude_columns=_METADATA_EXCLUDE_COLUMNS,
        name_template="fixed.csv",
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    zip_path = Path(next(o for o in result.outputs if o.kind == "export").ref["path"])
    names = sorted(n for n in _zip_names(zip_path) if n != "manifest.json")
    assert names == ["fixed-2.csv", "fixed-3.csv", "fixed.csv"]
    manifest = _zip_manifest(zip_path)
    assert {a["filename"] for a in manifest["artifacts"]} == set(names)
    project.close()


def test_project_file_destination_stores_content_addressed_blob(tmp_path: Path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="P")
    sheet_id, column_id, _title = _seed_pdf_tables_sheet(project)
    action = _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination={"kind": "project_file", "prefix": "exports/tables"},
        key="project_file@sha256:v1",
        group_by="table_index",
        exclude_columns=_METADATA_EXCLUDE_COLUMNS,
    )
    result = run_action_spec(project, action, project_id="p")
    assert result.status == "completed", result.errors
    ref = next(o for o in result.outputs if o.kind == "export").ref
    assert ref["kind"] == "export_project_file"
    assert ref["project_path"].startswith("exports/tables/")
    blob_hash = ref["blob_hash"]
    blob_row = project.db.execute(
        "SELECT * FROM blobs WHERE hash=?", (blob_hash,)
    ).fetchone()
    assert blob_row is not None
    with project.materialize_blob(blob_hash) as path:
        assert path.exists()

    # Replay reads the SAME blob back and matches.
    replay = run_action_spec(project, action, project_id="p")
    assert replay.status == "completed"
    assert replay.receipt_id == result.receipt_id
    project.close()
