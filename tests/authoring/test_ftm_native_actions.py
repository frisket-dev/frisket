"""Bundled FtM native declarations exercise the ordinary callable domain host.

Installed plugin transport is tested separately when callable admission lands.
"""

import io
import json
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from pydantic import ValidationError

from frisket.actions.core import RegisteredAction
from frisket.actions.entity_package_types import (
    ExportedEntityPackage,
    ImportedEntityDataset,
)
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.callable_action import run_typed_callable_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from frisket.plugins.action_loader import load_action_module as _load_module

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_ROOT = ROOT / "src/frisket/authoring/bundled_plugins/frisket.ftm"
GOLDENS = ROOT / "tests/goldens/ftm_bundled_plugin"


@pytest.fixture
def native(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT))
    return _load_module(PLUGIN_ROOT, PLUGIN_ROOT / "ftm_actions.py")


def _bind(definition, params, key):
    registered = RegisteredAction(f"frisket.ftm.{definition.name}", definition)
    return BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params=params,
            idempotency_key=key,
        ),
    )


def test_native_params_and_generated_forms_are_the_only_authored_contract(native):
    imported = native.FtmImportParams(source_path="case.jsonl")
    assert imported.dataset_name is None
    with pytest.raises(ValidationError):
        native.FtmImportParams(path="case.jsonl")
    with pytest.raises(ValidationError):
        native.FtmExportParams(rowsets=[], mappings=[])
    for definition in (native.FTM_IMPORT, native.FTM_EXPORT):
        registered = RegisteredAction(f"frisket.ftm.{definition.name}", definition)
        entry = registered.catalog_entry()
        assert entry["ui_hints"]["form"] == "generated"
        assert set(entry["required_capabilities"]) == {"project:read", "project:write"}
        assert entry["writes_project"] is True
        assert entry["cost_policy"]["requires_confirmation"] is False
    exported = native.FtmExportParams.model_json_schema()["properties"]
    assert exported["filename"]["default"] == "entities.ftm.zip"
    assert exported["validate_entities"]["default"] is True


@pytest.mark.parametrize("validate", [False, True])
def test_native_import_and_export_return_real_dataset_and_golden_package(
    native, tmp_path, validate
):
    pytest.importorskip("followthemoney")
    from frisket.features.followthemoney.import_planner import (
        plan_followthemoney_import,
    )
    from frisket.features.followthemoney.migration_harness import (
        FTM_HARNESS_PROJECT_ID,
        _canonical_export_inputs,
    )

    source = GOLDENS / "case.ftm.json"
    with closing(Project.create(tmp_path / "project")) as project:
        import_request = _bind(
            native.FTM_IMPORT,
            {"source_path": str(source), "dataset_name": "Native case"},
            "import",
        )
        imported = run_typed_callable_action(
            project, FTM_HARNESS_PROJECT_ID, import_request
        )
        assert imported.status == "completed", imported.errors
        dataset = ImportedEntityDataset.model_validate(imported.value)
        assert dataset.dataset_name == "Native case"
        assert {sheet.kind for sheet in dataset.sheets} == {
            "entity",
            "relationship",
            "unsupported",
        }
        assert len(dataset.sheets) == 4
        plan = plan_followthemoney_import(
            source.read_bytes(), dataset_name="Native case"
        )
        selected, mappings, _ = _canonical_export_inputs(project, plan)
        export_request = _bind(
            native.FTM_EXPORT,
            {
                "rowsets": selected["rowsets"],
                "mappings": mappings,
                "filename": "native-case.zip",
                "validate_entities": validate,
            },
            "export",
        )
        exported = run_typed_callable_action(
            project, FTM_HARNESS_PROJECT_ID, export_request
        )
        assert exported.status == "completed", exported.errors
        package = ExportedEntityPackage.model_validate(exported.value)
        assert package.filename == "native-case.zip"
        receipt = ReceiptStore(project).parsed_by_id(exported.receipt_id)
        assert receipt.exports == [package.model_dump(mode="json")]
        assert exported.outputs[0].kind == "export"
        with zipfile.ZipFile(
            io.BytesIO(project.read_blob(package.blob_hash))
        ) as archive:
            assert (
                archive.read("entities.ftm.jsonl")
                == (GOLDENS / "export.json").read_bytes()
            )
            assert json.loads(archive.read("manifest.json"))["validate"] is validate
            assert json.loads(archive.read("source_refs.json"))["entry_count"] > 0
        assert (
            run_typed_callable_action(project, FTM_HARNESS_PROJECT_ID, import_request)
            == imported
        )
        assert (
            run_typed_callable_action(project, FTM_HARNESS_PROJECT_ID, export_request)
            == exported
        )


@pytest.mark.parametrize("name", ["import", "export"])
def test_native_handlers_surface_real_missing_extra_refusal(
    native, tmp_path, monkeypatch, name
):
    from frisket.engine.executor import entity_package

    monkeypatch.setattr(
        entity_package,
        "entities_available",
        lambda: (False, "Install frisket[entities]."),
    )
    definition = native.FTM_IMPORT if name == "import" else native.FTM_EXPORT
    params = (
        {"source_path": "not-read.jsonl"}
        if name == "import"
        else {"rowsets": [{"sheet_id": 1}], "mappings": [{"schema": "Person"}]}
    )
    with closing(Project.create(tmp_path / "project")) as project:
        result = run_typed_callable_action(
            project, "p", _bind(definition, params, name)
        )
        assert result.status == "failed"
        assert result.errors[0].code == "followthemoney_unavailable"
        assert not result.outputs
        assert not ReceiptStore(project).parsed_by_id(result.receipt_id).exports
