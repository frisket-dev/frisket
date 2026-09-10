from __future__ import annotations

from pathlib import Path

import pytest

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.sdk import (
    ActionParams,
    ColumnTablesExport,
    ColumnTablesExporter,
    ColumnTablesLocalDirDestination,
)
from frisket.engine.executor import resolve_map_preview, run_action_spec
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.contracts.action import Receipt, ReceiptIO
from tests.engine.test_export_column_tables_executor import (
    _export_action,
    _seed_scalar_list_sheet,
    _zip_csv,
)


def _request(project, tmp_path, destination_kind):
    sheet_id, column_id = _seed_scalar_list_sheet(project)
    directory = tmp_path / "exports"
    directory.mkdir()
    destination = (
        {"kind": "local_dir", "path": str(directory)}
        if destination_kind == "local_dir"
        else {"kind": "project_file", "prefix": "exports/tables"}
    )
    return _export_action(
        sheet_id=sheet_id,
        column_id=column_id,
        destination=destination,
        key="column-tables-once",
    )


def _counts(project):
    return {
        table: project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ops", "receipts", "blobs")
    }


def _artifact_path(project, ref):
    if ref["destination_kind"] == "local_dir":
        return Path(ref["path"])
    with project.materialize_blob(ref["blob_hash"]) as path:
        return path


@pytest.mark.parametrize(
    ("status", "placement", "kind", "retained"),
    [
        ("completed", "exports", "export_project_file", True),
        ("failed", "exports", "export_project_file", True),
        ("cancelled", "exports", "export_project_file", True),
        ("completed", "inputs", "export_project_file", False),
        ("completed", "exports", "export_artifact", False),
    ],
)
def test_published_project_export_receipts_root_blobs_across_handler_outcomes(
    tmp_path, status, placement, kind, retained
):
    project = Project.create(tmp_path / "project")
    try:
        digest = project.add_blob(b"export bytes", filename="package.zip")
        ref = {"kind": kind, "blob_hash": digest}
        receipt = Receipt(
            receipt_id="receipt-export",
            project_id="p",
            action_id="export-test",
            action_kind="export.column_tables",
            status=status,
            exports=[ref] if placement == "exports" else [],
            inputs=[ReceiptIO(name="source", ref=ref)] if placement == "inputs" else [],
        )
        ReceiptStore(project).insert_finished(receipt)
        project.gc_blobs()
        assert bool(project.db.execute("SELECT 1 FROM blobs").fetchone()) is retained
        project.db.execute("DELETE FROM receipts WHERE id=?", (receipt.receipt_id,))
        project.db.commit()
        project.gc_blobs()
        assert project.db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0
    finally:
        project.close()


@pytest.mark.parametrize("destination_kind", ["local_dir", "project_file"])
def test_preview_refuses_before_source_read_or_artifact_write(
    tmp_path, monkeypatch, destination_kind
):
    from frisket.engine.executor.action_families import exports_column_tables as exports

    project = Project.create(tmp_path / "project")
    try:
        request = _request(project, tmp_path, destination_kind)
        before = _counts(project)

        def refuse_read(*args, **kwargs):
            raise AssertionError("preview must refuse before invoking the exporter")

        monkeypatch.setattr(exports, "_resolve_export_column_tables", refuse_read)
        preview = resolve_map_preview(project, request)
        assert preview.code == "unsupported_action_kind"
        assert _counts(project) == before
        assert list((tmp_path / "exports").iterdir()) == []
    finally:
        project.close()


@pytest.mark.parametrize("destination_kind", ["local_dir", "project_file"])
def test_replay_survives_deleted_source_and_refuses_retired_envelope(
    tmp_path, monkeypatch, destination_kind
):
    from frisket.engine.executor.action_families import exports_column_tables as exports

    project = Project.create(tmp_path / "project")
    try:
        request = _request(project, tmp_path, destination_kind)
        first = run_action_spec(project, request, project_id="p")
        assert first.status == "completed", first.errors
        artifact = _artifact_path(project, first.outputs[0].ref)
        original_bytes = artifact.read_bytes()
        project.delete_sheet(request["params"]["sheet_id"])
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        before = _counts(project)

        def refuse_read(*args, **kwargs):
            raise AssertionError("replay must not consult the deleted source")

        monkeypatch.setattr(exports, "_resolve_export_column_tables", refuse_read)
        replay = run_action_spec(project, request, project_id="p")
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert artifact.read_bytes() == original_bytes
        retired = {
            "schema_version": "frisket.action.v2",
            "kind": "export.column_tables",
            "capabilities": ["project:read", "project:write"],
            "params": request["params"],
            "idempotency_key": request["idempotency_key"],
        }
        refused = run_action_spec(project, retired, project_id="p")
        assert refused.status == "failed"
        assert refused.errors[0].code == "invalid_action_request"
        changed = {
            **request,
            "params": {**request["params"], "name_template": "changed.csv"},
        }
        conflict = run_action_spec(project, changed, project_id="p")
        assert conflict.status == "failed"
        assert conflict.errors[0].code == "idempotency_conflict"
        assert _counts(project) == before
    finally:
        project.close()


@pytest.mark.parametrize("defect", ["metadata", "missing", "tampered"])
def test_project_blob_replay_checks_metadata_and_actual_bytes(tmp_path, defect):
    project = Project.create(tmp_path / "project")
    try:
        request = _request(project, tmp_path, "project_file")
        first = run_action_spec(project, request, project_id="p")
        assert first.status == "completed", first.errors
        ref = first.outputs[0].ref
        path = _artifact_path(project, ref)
        if defect == "metadata":
            project.db.execute("DELETE FROM blobs WHERE hash=?", (ref["blob_hash"],))
            project.db.commit()
        elif defect == "missing":
            path.unlink()
        else:
            raw = path.read_bytes()
            path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
        before = _counts(project)
        replay = run_action_spec(project, request, project_id="p")
        assert replay.status == "failed"
        assert replay.errors[0].code == (
            "export_artifact_mismatch"
            if defect == "tampered"
            else "export_artifact_missing"
        )
        assert _counts(project) == before
    finally:
        project.close()


@pytest.mark.parametrize("destination_kind", ["local_dir", "project_file"])
@pytest.mark.parametrize("winner_artifact", ["valid", "missing", "tampered"])
def test_inner_idempotency_race_validates_winner_and_cleans_losing_staging(
    tmp_path, monkeypatch, destination_kind, winner_artifact
):
    from frisket.engine.executor import action_lifecycle
    from frisket.engine.executor.action_families import exports_column_tables as exports

    project = Project.create(tmp_path / "project")
    try:
        request = _request(project, tmp_path, destination_kind)
        winner = run_action_spec(project, request, project_id="p")
        assert winner.status == "completed", winner.errors
        artifact = _artifact_path(project, winner.outputs[0].ref)
        if winner_artifact == "missing":
            artifact.unlink()
        elif winner_artifact == "tampered":
            original_bytes = artifact.read_bytes()
            artifact.write_bytes(bytes([original_bytes[0] ^ 1]) + original_bytes[1:])
        before = _counts(project)
        directory = tmp_path / "exports"
        before_files = {path.name: path.read_bytes() for path in directory.iterdir()}
        lookup = action_lifecycle._receipt_for_idempotency
        build_zip = exports._build_zip
        lookups = []
        renders = []

        def concurrent_lookup(target, key):
            lookups.append(target.db.in_transaction)
            if len(lookups) == 1:
                return None
            if destination_kind == "local_dir":
                assert len(list(directory.glob(".tags.zip.*.tmp"))) == 1
            return lookup(target, key)

        def observe_render(*args, **kwargs):
            renders.append(True)
            return build_zip(*args, **kwargs)

        monkeypatch.setattr(
            action_lifecycle, "_receipt_for_idempotency", concurrent_lookup
        )
        monkeypatch.setattr(exports, "_build_zip", observe_render)
        result = run_action_spec(project, request, project_id="p")
        assert lookups == [False, True]
        assert renders == [True]
        if winner_artifact == "valid":
            assert result.status == "completed", result.errors
            assert result.receipt_id == winner.receipt_id
        else:
            assert result.status == "failed"
            assert result.errors[0].code == (
                "export_artifact_missing"
                if winner_artifact == "missing"
                else "export_artifact_mismatch"
            )
        assert _counts(project) == before
        assert {
            path.name: path.read_bytes() for path in directory.iterdir()
        } == before_files
    finally:
        project.close()


class PackageParams(ActionParams):
    owner: int
    field: int
    folder: str


def reconstructed_package(
    params: PackageParams, exporter: ColumnTablesExporter
) -> ColumnTablesExport:
    result = exporter.write_column_tables(
        sheet_id=params.owner,
        column_id=params.field,
        destination=ColumnTablesLocalDirDestination(
            kind="local_dir", path=str(Path(params.folder) / "nested")
        ),
        name_template="derived.csv",
    )
    reconstructed = ColumnTablesExport.model_validate(result.model_dump())
    reconstructed.manifest["artifact_count"] = 999
    return reconstructed


def test_renamed_derived_arguments_and_reconstructed_result_keep_host_facts(tmp_path):
    from frisket.engine.executor.action_families.exports_column_tables import (
        _run_typed_export_column_tables,
    )

    project = Project.create(tmp_path / "project")
    try:
        sheet_id, column_id = _seed_scalar_list_sheet(project)
        destination = tmp_path / "nested"
        destination.mkdir()
        definition = action(
            name="package",
            title="Package",
            description="Export a column through derived arguments.",
            category=ActionCategory.CONVERT,
            run=reconstructed_package,
        )
        registered = ActionRegistry(
            (ActionNamespace("custom", actions=(definition,)),)
        ).get("custom.package")
        bound = BoundTypedActionRequest.bind(
            registered,
            ActionRequest(
                action_id="custom.package",
                scope={"kind": "project"},
                params={"owner": sheet_id, "field": column_id, "folder": str(tmp_path)},
                idempotency_key="derived-package",
            ),
        )
        result = _run_typed_export_column_tables(project, "p", bound)
        assert result.status == "completed", result.errors
        ref = result.outputs[0].ref
        assert ref["sheet_id"] == sheet_id
        assert ref["column_id"] == column_id
        assert Path(ref["path"]).parent == destination
        assert _zip_csv(Path(ref["path"]), "derived.csv") == [
            {"value": "red"},
            {"value": "green"},
            {"value": "blue"},
        ]
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        manifest = next(
            evidence.ref
            for evidence in receipt.evidence
            if evidence.ref["kind"] == "column_tables_export_manifest"
        )
        assert manifest["artifact_count"] == 1
        assert manifest["name_template"] == "derived.csv"
    finally:
        project.close()
