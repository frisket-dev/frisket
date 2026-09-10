"""Real FtM primitives preserve complete datasets and deliver retained ZIPs."""

import io
import hashlib
import json
import zipfile
from contextlib import closing

import pytest
from pydantic import BaseModel

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.entity_package_types import (
    EntityRowset,
    ExportedEntityPackage,
    FollowTheMoneyExporter,
    FollowTheMoneyImporter,
    ImportedEntityDataset,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionParams, ActionRequest
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_inventory import (
    BoundLocalFile,
    ExecutorDeps,
    ImportWorkloadLimits,
)
from frisket.engine.executor.callable_action import run_typed_callable_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


class Params(ActionParams):
    document: str
    label: str = "Case"
    fail: bool = False


class Report(BaseModel):
    dataset: ImportedEntityDataset
    package: ExportedEntityPackage


def _bound(handler, params, *, key="ftm"):
    registry = ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=(
                    action(
                        name="entities",
                        title="Entities",
                        description="Entity dataset",
                        category=ActionCategory.CONVERT,
                        run=handler,
                    ),
                ),
            ),
        )
    )
    request = ActionRequest(
        action_id="custom.entities",
        scope={"kind": "project"},
        params=params,
        idempotency_key=key,
    )
    return BoundTypedActionRequest.bind(
        registry.get(request.action_id), request
    ), registry


def _selection(dataset):
    rowsets, mappings = [], []
    for sheet in dataset.sheets:
        rowset = EntityRowset(sheet_id=sheet.sheet_id)
        rowsets.append(rowset)
        mappings.append(
            {
                "rowset": rowset.model_dump(),
                "schema": sheet.schema_name,
                "id_policy": {"kind": "row_ref"},
                "properties": {
                    name: {"column": name}
                    for name in sheet.column_ids
                    if not name.startswith("_ftm_")
                },
            }
        )
    return rowsets, mappings


def import_entities(
    params: Params, importer: FollowTheMoneyImporter
) -> ImportedEntityDataset:
    return importer.import_entities(
        source_path=params.document, dataset_name=params.label
    )


def roundtrip(
    params: Params, exporter: FollowTheMoneyExporter, importer: FollowTheMoneyImporter
) -> Report:
    dataset = importer.import_entities(
        source_path=params.document, dataset_name=params.label
    )
    rows, mappings = _selection(dataset)
    package = exporter.export_entities(rowsets=rows, mappings=mappings)
    if params.fail:
        raise RuntimeError("later authored failure")
    return Report(dataset=dataset, package=package)


@pytest.fixture
def entities(tmp_path):
    pytest.importorskip("followthemoney")
    values = [
        {
            "id": "person",
            "schema": "Person",
            "datasets": ["source"],
            "properties": {"name": ["Jane"], "alias": ["J", "Jay"]},
        },
        {"id": "company", "schema": "Company", "properties": {"name": ["Acme"]}},
        {
            "id": "membership",
            "schema": "Membership",
            "properties": {
                "member": ["person"],
                "organization": ["company"],
                "role": ["Director"],
            },
        },
        {
            "id": "address",
            "schema": "Address",
            "properties": {"full": ["1 Main Street"]},
        },
    ]
    path = tmp_path / "entities.jsonl"
    path.write_text("\n".join(json.dumps(value) for value in values))
    return path, values


def test_real_callable_import_preserves_schema_relationships_raw_and_undo(
    tmp_path, entities, monkeypatch
):
    from frisket.actions import system

    path, raw = entities
    with closing(Project.create(tmp_path / "project")) as project:
        bound, registry = _bound(import_entities, {"document": str(path)})
        monkeypatch.setattr(system, "ACTION_REGISTRY", registry)
        result = run_action_spec(
            project, bound.request.model_dump(mode="json"), project_id="p"
        )
        assert result.status == "completed", result.errors
        dataset = ImportedEntityDataset.model_validate(result.value)
        assert [sheet.schema_name for sheet in dataset.sheets] == [
            "Company",
            "Person",
            "Address",
            "Membership",
        ]
        assert {sheet.kind for sheet in dataset.sheets} == {
            "entity",
            "relationship",
            "unsupported",
        }
        assert len([output for output in result.outputs if output.kind == "sheet"]) == 4
        restored = []
        for sheet in dataset.sheets:
            restored.extend(
                project.get_values(
                    sheet.sheet_id,
                    sheet.column_ids["_ftm_raw_json"],
                    row_ids=list(sheet.row_ids),
                ).values()
            )
            assert all(
                row[0] == 1
                for row in project.db.execute(
                    "SELECT hidden FROM columns WHERE sheet_id=? AND name LIKE '_ftm_%'",
                    (sheet.sheet_id,),
                )
            )
        assert sorted(restored, key=lambda value: value["id"]) == sorted(
            raw, key=lambda value: value["id"]
        )
        receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
        assert receipt.op_ids == [dataset.op_id]
        assert next(
            item.ref for item in receipt.inputs if item.ref["kind"] == "local_file_read"
        )["sha256"]
        path.unlink()
        assert run_typed_callable_action(project, "p", bound) == result
        assert project.undo() == dataset.op_id
        replay = run_typed_callable_action(project, "p", bound)
        assert replay.status == "failed" and replay.errors[0].code == "stale_replay"
        assert project.redo() == dataset.op_id
        assert run_typed_callable_action(project, "p", bound) == result


def test_package_download_uses_existing_project_blob_route(client, entities):
    path, _ = entities
    pid = client.post("/api/projects", json={"name": "Entities"}).json()["id"]
    other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
    project = client.app.state.workspace.get(pid)
    bound, _ = _bound(roundtrip, {"document": str(path)})
    result = run_typed_callable_action(project, pid, bound)
    assert result.status == "completed", result.errors
    report = Report.model_validate(result.value)
    ref = ReceiptStore(project).parsed_by_id(result.receipt_id).exports[0]
    response = client.get(f"/api/projects/{pid}/blobs/{ref['blob_hash']}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.content == project.read_blob(report.package.blob_hash)
    assert (
        client.get(f"/api/projects/{other}/blobs/{ref['blob_hash']}").status_code == 404
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist()) == {
            "entities.ftm.jsonl",
            "validation_report.json",
            "mapping.json",
            "source_refs.json",
            "manifest.json",
        }
        assert len(archive.read("entities.ftm.jsonl").splitlines()) == 4
        assert json.loads(archive.read("source_refs.json"))["entry_count"] > 0
    assert run_typed_callable_action(project, pid, bound) == result


@pytest.mark.parametrize("fail", [False, True])
def test_committed_packages_survive_gc_and_later_handler_failure(
    tmp_path, entities, fail
):
    path, _ = entities
    with closing(Project.create(tmp_path / "project")) as project:
        observed = []

        def run(
            params: Params,
            importer: FollowTheMoneyImporter,
            exporter: FollowTheMoneyExporter,
        ) -> Report:
            dataset = importer.import_entities(
                source_path=params.document, dataset_name=params.label
            )
            rowsets, mappings = _selection(dataset)
            first = exporter.export_entities(rowsets=rowsets[:1], mappings=mappings[:1])
            second = exporter.export_entities(rowsets=rowsets, mappings=mappings)
            assert first.blob_hash != second.blob_hash
            observed.extend((first.blob_hash, second.blob_hash))
            project.gc_blobs()  # The receipt is still running, but both calls committed.
            assert all(project.read_blob(digest) for digest in observed)
            if params.fail:
                raise RuntimeError("after delivery")
            return Report(dataset=dataset, package=second)

        bound, _ = _bound(run, {"document": str(path), "fail": fail})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == ("failed" if fail else "completed"), result.errors
        assert len(ReceiptStore(project).parsed_by_id(result.receipt_id).exports) == 2
        project.gc_blobs()
        assert all(project.read_blob(digest) for digest in observed)
        assert run_typed_callable_action(project, "p", bound) == result


@pytest.mark.parametrize("failure", ["apply", "receipt", "commit"])
def test_late_import_failure_preserves_previous_effect_and_receipt(
    tmp_path, entities, monkeypatch, failure
):
    from frisket.engine.executor import plugin_write_apply
    from tests.engine.test_export_delivery_failure import _wrap_project_db_commit

    path, _ = entities
    prior_path = tmp_path / "prior.jsonl"
    prior_path.write_text(
        json.dumps(
            {"id": "prior", "schema": "LegalEntity", "properties": {"name": ["Prior"]}}
        )
    )
    with closing(Project.create(tmp_path / "project")) as project:
        wrapper = _wrap_project_db_commit(monkeypatch, project)

        def run(
            params: Params, importer: FollowTheMoneyImporter
        ) -> ImportedEntityDataset:
            prior = importer.import_entities(
                source_path=str(prior_path), dataset_name="Prior"
            )
            before = tuple(project.db.iterdump())
            with monkeypatch.context() as patch:
                if failure == "apply":
                    original = plugin_write_apply.json.dumps

                    def reject(value, *args, **kwargs):
                        if value == "Director":
                            raise RuntimeError("late relationship cell")
                        return original(value, *args, **kwargs)

                    patch.setattr(plugin_write_apply.json, "dumps", reject)
                elif failure == "receipt":
                    update = ReceiptStore.update_body_status
                    patch.setattr(
                        ReceiptStore,
                        "update_body_status",
                        lambda self, receipt, **kwargs: (
                            False
                            if len(receipt.op_ids) == 2
                            else update(self, receipt, **kwargs)
                        ),
                    )
                else:
                    wrapper.fail_commit_at = wrapper.commit_count + 1
                with pytest.raises(Exception):
                    importer.import_entities(
                        source_path=params.document, dataset_name="Next"
                    )
            assert tuple(project.db.iterdump()) == before
            return prior

        bound, _ = _bound(run, {"document": str(path)})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "completed", result.errors
        assert len(result.op_ids) == 1
        assert len([output for output in result.outputs if output.kind == "sheet"]) == 1


def test_unavailable_extra_refuses_before_source_read(tmp_path, monkeypatch):
    from frisket.engine.executor import entity_package

    monkeypatch.setattr(
        entity_package,
        "entities_available",
        lambda: (False, "Install frisket-data[entities]."),
    )
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": "not-readable"})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "followthemoney_unavailable"
        assert not result.outputs


@pytest.mark.parametrize("content", [b"", b"{not-json\n[]\n", b"\xff"])
def test_empty_or_all_invalid_import_refuses_before_write(tmp_path, entities, content):
    path = tmp_path / "invalid.jsonl"
    path.write_bytes(content)
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": str(path)})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "invalid_ftm_import"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0


def test_import_row_budget_counts_every_sheet_kind_before_write(tmp_path, entities):
    path, _ = entities
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": str(path)})
        result = run_typed_callable_action(
            project,
            "p",
            bound,
            deps=ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=3)),
        )
        assert result.status == "failed"
        assert result.errors[0].code == "import_workload_limit_exceeded"
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert project.db.execute("SELECT COUNT(*) FROM ops").fetchone()[0] == 0


def test_import_row_budget_stops_incremental_source_read(tmp_path, entities):
    del entities
    content = b"".join(
        json.dumps(
            {
                "id": f"person-{index}",
                "schema": "Person",
                "properties": {"name": [f"Person {index}"]},
            }
        ).encode()
        + b"\n"
        for index in range(20_000)
    )
    borrowed = io.BytesIO(content)
    source_path = "admitted.jsonl"
    deps = ExecutorDeps(
        local_file_sources={
            source_path: BoundLocalFile(
                borrowed, "sha256:" + hashlib.sha256(content).hexdigest()
            )
        },
        import_workload_limits=ImportWorkloadLimits(max_rows=1),
    )
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": source_path})
        result = run_typed_callable_action(project, "p", bound, deps=deps)
        assert result.status == "failed"
        assert result.errors[0].code == "import_workload_limit_exceeded"
        assert borrowed.tell() < len(content)
        assert not borrowed.closed


def test_import_retains_bounded_diagnostics_with_explicit_summary(
    tmp_path, entities, monkeypatch
):
    from frisket.engine.executor import entity_package

    del entities
    monkeypatch.setattr(entity_package, "MAX_RETAINED_IMPORT_DIAGNOSTICS", 3)
    monkeypatch.setattr(entity_package, "MAX_RETAINED_IMPORT_DIAGNOSTIC_CHARS", 40)
    valid = json.dumps(
        {
            "id": "person",
            "schema": "Person",
            "properties": {"name": ["Jane"]},
        }
    )
    path = tmp_path / "mixed.jsonl"
    oversized_schema = "Unknown" + "x" * 100
    invalid = json.dumps({"id": "invalid", "schema": oversized_schema})
    path.write_text("\n".join([valid, invalid, *(["{bad"] * 5)]))
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": str(path)})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "completed", result.errors
        dataset = ImportedEntityDataset.model_validate(result.value)
        assert len(dataset.diagnostics) == 3
        assert [item["code"] for item in dataset.diagnostics] == [
            "unsupported_schema",
            "invalid_json",
            "diagnostics_truncated",
        ]
        assert dataset.diagnostics[-1]["omitted_count"] == 4
        assert len(dataset.diagnostics[0]["message"]) == 40
        assert len(dataset.diagnostics[0]["schema"]) == 40


def test_apply_failure_is_not_classified_as_invalid_input(
    tmp_path, entities, monkeypatch
):
    from frisket.engine.executor import entity_package
    from frisket.engine.executor.plugin_write_apply import WritePlanApplyError

    path, _ = entities

    def fail_apply(*args, **kwargs):
        del args, kwargs
        raise WritePlanApplyError("internal database detail")

    monkeypatch.setattr(entity_package, "apply_additive_tables", fail_apply)
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": str(path)})
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == "action_failed"
        assert "internal database detail" not in result.errors[0].message
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0


def test_import_plan_chunks_existing_append_bound_without_losing_order(tmp_path):
    from frisket.contracts.plugin_write_plan import MAX_ROWS_PER_APPEND
    from frisket.engine.executor.plugin_write_apply import apply_additive_tables
    from frisket.features.followthemoney.write_plan import import_plan_to_write_plan

    count = MAX_ROWS_PER_APPEND + 1
    plan = import_plan_to_write_plan(
        {
            "sheets": [
                {
                    "sheet_name": "Entities",
                    "columns": [{"name": "id", "type": "text"}],
                    "rows": [{"values": {"id": str(index)}} for index in range(count)],
                }
            ]
        }
    )
    assert [len(op.rows) for op in plan.ops if op.op == "append_rows"] == [
        MAX_ROWS_PER_APPEND,
        1,
    ]
    with closing(Project.create(tmp_path / "project")) as project:
        applied = apply_additive_tables(
            project, plan, operation_kind="test.import", label="Import", provenance={}
        )
        rows = project.db.execute(
            "SELECT value FROM cells JOIN rows ON rows.id=cells.row_id ORDER BY position"
        ).fetchall()
        assert [json.loads(row[0]) for row in rows] == [
            str(index) for index in range(count)
        ]
        assert len(applied.row_ids["s0"]) == count


@pytest.mark.parametrize("changed", [False, True])
def test_import_uses_actual_admitted_method_path_and_retains_borrowed_handle(
    tmp_path, entities, changed
):
    from frisket.engine.executor.action_inventory import BoundLocalFile, ExecutorDeps

    path, _ = entities
    content = path.read_bytes()
    borrowed = io.BytesIO(content + (b" " if changed else b""))
    admitted_path = "not-a-host-path.jsonl"
    deps = ExecutorDeps(
        local_file_sources={
            admitted_path: BoundLocalFile(
                borrowed, "sha256:" + hashlib.sha256(content).hexdigest()
            )
        }
    )
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(import_entities, {"document": admitted_path})
        result = run_typed_callable_action(project, "p", bound, deps=deps)
        assert result.status == ("failed" if changed else "completed"), result.errors
        if changed:
            assert result.errors[0].code == "invalid_file_source"
            assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
        assert not borrowed.closed


@pytest.mark.parametrize("failure", ["receipt", "commit"])
def test_failed_package_publication_preserves_shared_blob_and_prior_receipt(
    tmp_path, entities, monkeypatch, failure
):
    from tests.engine.test_export_delivery_failure import _wrap_project_db_commit

    path, _ = entities
    with closing(Project.create(tmp_path / "project")) as project:
        original_bound, _ = _bound(roundtrip, {"document": str(path)})
        original = run_typed_callable_action(project, "p", original_bound)
        assert original.status == "completed", original.errors
        report = Report.model_validate(original.value)
        content = project.read_blob(report.package.blob_hash)
        wrapper = _wrap_project_db_commit(monkeypatch, project)

        def run(params: Params, exporter: FollowTheMoneyExporter) -> dict[str, bool]:
            before = tuple(project.db.iterdump())
            rowsets, mappings = _selection(report.dataset)
            with monkeypatch.context() as patch:
                if failure == "receipt":
                    update = ReceiptStore.update_body_status
                    patch.setattr(
                        ReceiptStore,
                        "update_body_status",
                        lambda self, receipt, **kwargs: (
                            False
                            if receipt.exports
                            else update(self, receipt, **kwargs)
                        ),
                    )
                else:
                    wrapper.fail_commit_at = wrapper.commit_count + 1
                with pytest.raises(Exception):
                    exporter.export_entities(rowsets=rowsets, mappings=mappings)
            assert tuple(project.db.iterdump()) == before
            assert project.read_blob(report.package.blob_hash) == content
            return {"retained": True}

        bound, _ = _bound(run, {"document": "unused"}, key="second")
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "completed", result.errors
        assert not ReceiptStore(project).parsed_by_id(result.receipt_id).exports
        project.gc_blobs()
        assert project.read_blob(report.package.blob_hash) == content
        assert run_typed_callable_action(project, "p", original_bound) == original


@pytest.mark.parametrize("missing", [False, True])
def test_export_replay_validates_retained_bytes_without_rerunning_handler(
    tmp_path, entities, monkeypatch, missing
):
    from frisket.engine.store.blob_backend import BlobNotFoundError

    path, _ = entities
    with closing(Project.create(tmp_path / "project")) as project:
        bound, _ = _bound(roundtrip, {"document": str(path)})
        original = run_typed_callable_action(project, "p", bound)
        assert original.status == "completed", original.errors
        before = tuple(project.db.iterdump())

        def read_blob(*args):
            if missing:
                raise BlobNotFoundError("missing")
            return b"corrupt"

        monkeypatch.setattr(Project, "read_blob", read_blob)
        result = run_typed_callable_action(project, "p", bound)
        assert result.status == "failed"
        assert result.errors[0].code == (
            "export_artifact_missing" if missing else "export_artifact_mismatch"
        )
        assert tuple(project.db.iterdump()) == before
