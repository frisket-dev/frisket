from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    create_sheet,
)
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    DynamicTableResult,
    RuntimeImporter,
    RuntimeImportSource,
)
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring.workbench.plugin_runtime import disable_workbench_plugin
from frisket.authoring.workbench.plugin_subprocess_importers import _ImporterPlanBuilder
from frisket.engine.executor import ExecutorDeps, ImportWorkloadLimits, run_action_spec
from frisket.engine.executor.actions import resolve_map_preview
from frisket.engine.executor import runtime_import_read
from frisket.engine.executor.table_action import run_typed_create_sheet_action
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


@pytest.fixture
def runtime(tmp_path):
    _reset_default_registry_for_tests()
    plugin_id, kind, handler_key = (
        "runtime.test",
        "runtime.test.importer",
        "runtime.test:read",
    )
    calls = []
    plan = {
        "schemaVersion": "frisket.runtime_importer_plan.v1",
        "columns": [
            {"name": "value", "type": "integer", "format": "filesize", "hidden": True}
        ],
        "rows": [{"value": 1}, {"value": 2}],
        "warnings": ["Import warning"],
        "diagnostics": [
            {
                "level": "warning",
                "code": "reported",
                "message": "Import warning",
                "attach": {"line": 2, "secret_extra": "discarded"},
            }
        ],
        "streaming": {"row_count": 999, "spooled": True},
    }

    def handler(payload):
        calls.append(deepcopy(payload))
        payload["handlerParams"]["mutated"] = "not an admitted argument"
        return plan

    register_trusted_backend_handler(handler_key, handler)
    default_registry().register_runtime_binding(
        "importers",
        kind,
        handler_key=handler_key,
        handler=handler,
        plugin=plugin_id,
    )
    with closing(Project.create(tmp_path / "project")) as project:
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=plugin_id,
            runtime_bindings={"importers": {kind: handler_key}},
            project_id="p",
        )
        request = {
            "action_id": "import.runtime",
            "scope": {"kind": "project"},
            "sheet_name": "Imported",
            "idempotency_key": "runtime-import",
            "params": {
                "importer_kind": kind,
                "source": {
                    "kind": "runtime",
                    "label": "input.ndjson",
                    "fingerprint": "declared",
                },
                "handler_params": {"option": "value"},
            },
        }
        yield SimpleNamespace(
            project=project,
            request=request,
            calls=calls,
            plan=plan,
            plugin_id=plugin_id,
            kind=kind,
            handler_key=handler_key,
        )
    unregister_trusted_backend_handler(handler_key)
    _reset_default_registry_for_tests()


def _fact(project, receipt_id):
    receipt = ReceiptStore(project).parsed_by_id(receipt_id)
    return next(
        item.ref
        for item in receipt.inputs
        if item.ref.get("kind") == "workbench_runtime_binding"
    )


def _runtime_receipts(project):
    return project.db.execute(
        "SELECT COUNT(*) FROM receipts WHERE action_kind='import.runtime'"
    ).fetchone()[0]


def test_runtime_quota_warnings_actual_facts_and_source_independent_replay(
    runtime, monkeypatch
):
    project, request = runtime.project, runtime.request
    limited = run_action_spec(
        project,
        request,
        project_id="p",
        deps=ExecutorDeps(
            import_workload_limits=ImportWorkloadLimits(max_rows=1),
        ),
    )
    assert limited.errors[0].code == "import_workload_limit_exceeded"
    assert project.sheets(include_hidden=True) == []
    assert _runtime_receipts(project) == 0
    result = run_action_spec(project, request, project_id="p")
    assert result.status == "completed", result.errors
    assert result.warnings == ["Import warning"]
    fact = _fact(project, result.receipt_id)
    assert fact["row_count"] == 2
    assert (
        "streaming" not in fact
    )  # The in-process handler's 999 is not observed evidence.
    assert fact["declared_source"] == request["params"]["source"]
    assert fact["source_fingerprint_verified"] is False
    assert fact["diagnostics"][0]["attach"] == {"line": 2}
    column = project.db.execute(
        "SELECT name, type, format, hidden FROM columns"
    ).fetchone()
    assert tuple(column) == ("value", "integer", "filesize", 1)

    def must_not_reopen(*args, **kwargs):
        raise AssertionError(
            "A completed request must replay before binding/source admission"
        )

    monkeypatch.setattr(runtime_import_read, "project_runtime_binding", must_not_reopen)
    replay = run_action_spec(project, request, project_id="p")
    assert replay.receipt_id == result.receipt_id
    changed = deepcopy(request)
    changed["params"]["handler_params"]["option"] = "changed"
    assert (
        run_action_spec(project, changed, project_id="p").errors[0].code
        == "idempotency_conflict"
    )
    sheet_id = next(
        output.sheet_id for output in result.outputs if output.kind == "sheet"
    )
    project.delete_sheet(sheet_id)
    assert (
        run_action_spec(project, request, project_id="p").errors[0].code
        == "stale_replay"
    )
    assert len(runtime.calls) == 2


def test_runtime_project_admission_and_preview_refuse_before_handler(runtime, tmp_path):
    assert (
        resolve_map_preview(runtime.project, runtime.request).code
        == "unsupported_action_kind"
    )
    with closing(Project.create(tmp_path / "other")) as other:
        refused = run_action_spec(other, runtime.request, project_id="other")
        assert refused.errors[0].code == "unsupported_runtime_importer"
        assert other.sheets(include_hidden=True) == []
    disable_workbench_plugin(
        runtime.project, project_id="p", plugin_id=runtime.plugin_id
    )
    refused = run_action_spec(runtime.project, runtime.request, project_id="p")
    assert refused.errors[0].code == "unsupported_runtime_importer"
    assert runtime.calls == []


class DerivedParams(ActionParams):
    route: str
    title: str
    option: str


def test_runtime_derived_arguments_and_reconstructed_results_cannot_forge_facts(
    runtime,
):
    marker = "private-derived-marker"

    def produce(params: DerivedParams, reader: RuntimeImporter) -> DynamicTableResult:
        table = reader.read(
            params.route,
            source=RuntimeImportSource(kind="runtime", label=params.title),
            handler_params={"actual_option": params.option + marker},
        )
        runtime.plan["diagnostics"][0]["message"] = "forged after admission"
        return DynamicTableResult(
            schema=table.schema,
            rows=table.rows,
            warnings=table.warnings,
            source={"kind": "inline", "label": "forged", "fingerprint": "forged"},
        )

    registered = ActionRegistry(
        (
            ActionNamespace(
                "example",
                actions=(
                    action(
                        name="runtime",
                        title="Runtime",
                        description="Read an admitted importer",
                        category=ActionCategory.CONVERT,
                        run=create_sheet(produce),
                    ),
                ),
            ),
        )
    ).get("example.runtime")
    hashes = []
    for index, option in enumerate(("one", "two")):
        request = ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            sheet_name=f"Derived {index}",
            params={"route": runtime.kind, "title": "Actual", "option": option},
            output_names={"value": "renamed"},
            idempotency_key=f"derived-{index}",
        )
        result = run_typed_create_sheet_action(
            runtime.project, "p", BoundTypedActionRequest.bind(registered, request)
        )
        assert result.status == "completed", result.errors
        fact = _fact(runtime.project, result.receipt_id)
        expected = hashlib.sha256(
            json.dumps(
                {"actual_option": option + marker},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert fact["handler_params_sha256"] == "sha256:" + expected
        hashes.append(fact["handler_params_sha256"])
        assert fact["declared_source"]["label"] == "Actual"
        assert fact["binding_kind"] == runtime.kind
        assert fact["row_count"] == 2
        assert "handler_params" not in fact
        assert runtime.calls[-1]["sheetName"] == f"Derived {index}"
        receipt_text = runtime.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
        ).fetchone()[0]
        op_text = runtime.project.db.execute(
            "SELECT spec FROM ops WHERE id=?", (result.op_ids[0],)
        ).fetchone()[0]
        assert marker not in receipt_text and marker not in op_text
        assert '"params"' not in op_text
        if index == 0:
            assert fact["diagnostics"][0]["message"] == "Import warning"
    assert len(set(hashes)) == 2


def _spooled_importer(runtime, monkeypatch, count=2):
    builders = []
    binding = SimpleNamespace(
        plugin=runtime.plugin_id,
        kind=runtime.kind,
        handler_key=runtime.handler_key,
        handler_api="plugin_importer_subprocess",
    )
    monkeypatch.setattr(
        runtime_import_read, "project_runtime_binding", lambda *args, **kwargs: binding
    )
    # This fixture supplies a synthetic spool, not an executable package.
    # Live dispatch integrity is covered by runtime importer preview admission tests.
    monkeypatch.setattr(
        runtime_import_read,
        "runtime_importer_dispatch_error",
        lambda *args, **kwargs: None,
    )

    def read(project, **kwargs):
        builder = _ImporterPlanBuilder(project, binding=binding, source_label="spooled")
        builders.append(builder)
        builder.handle_frame(
            {"type": "schema", "columns": [{"name": "value", "type": "integer"}]}
        )
        for value in range(count):
            builder.handle_frame({"type": "row", "row": {"value": value}})
        builder.handle_frame({"type": "done", "row_count": count})
        return builder.finish()

    monkeypatch.setattr(runtime_import_read, "run_plugin_importer_subprocess", read)
    return builders


@pytest.mark.parametrize("failure", ["schema", "quota", "receipt"])
def test_runtime_owned_spool_closes_and_publication_rolls_back(
    runtime, monkeypatch, failure
):
    builders = _spooled_importer(runtime, monkeypatch, count=501)
    request = deepcopy(runtime.request)
    deps = ExecutorDeps()
    if failure == "schema":
        request["output_names"] = {"unknown": "invalid"}
    elif failure == "quota":
        deps = ExecutorDeps(import_workload_limits=ImportWorkloadLimits(max_rows=500))
    else:
        original = ReceiptStore.insert_completed

        def refuse_receipt(self, receipt, **kwargs):
            if receipt.action_kind == "import.runtime":
                raise OSError("receipt write failed")
            return original(self, receipt, **kwargs)

        monkeypatch.setattr(ReceiptStore, "insert_completed", refuse_receipt)
    result = run_action_spec(runtime.project, request, project_id="p", deps=deps)
    assert result.status == "failed"
    assert builders and all(builder.stream.closed for builder in builders)
    assert runtime.project.sheets(include_hidden=True) == []
    assert _runtime_receipts(runtime.project) == 0
    assert result.outputs == []


def test_runtime_owned_spool_closes_without_starting_rows(runtime, monkeypatch):
    builders = _spooled_importer(runtime, monkeypatch)
    with closing(
        runtime_import_read.AdmittedRuntimeImporter(
            runtime.project,
            project_id="p",
            sheet_name="Abandoned",
            action_id="import.runtime",
        )
    ) as reader:
        table = reader.read(
            runtime.kind, source=RuntimeImportSource(kind="runtime"), handler_params={}
        )
        assert not builders[0].stream.closed
        table.rows.close()
    assert builders[0].stream.closed
    assert runtime.project.sheets(include_hidden=True) == []


@pytest.mark.parametrize("change", ["old_envelope", "extra_mode", "nan", "oversized"])
def test_runtime_request_validation_is_closed_and_bounded(runtime, change):
    request = deepcopy(runtime.request)
    if change == "old_envelope":
        request = {
            "schema_version": "frisket.action.v2",
            "kind": "import.runtime",
            "capabilities": ["project:write"],
            "idempotency_key": "old",
            "params": {
                **request["params"],
                "mode": "create_sheet",
                "sheet_name": "Old",
            },
        }
    elif change == "extra_mode":
        request["params"]["mode"] = "create_sheet"
    elif change == "nan":
        request["params"]["handler_params"] = {"value": float("nan")}
    else:
        request["params"]["handler_params"] = {"value": "x" * 100_000}
    assert not validate_root_action(request).ok
    result = run_action_spec(runtime.project, request, project_id="p")
    assert result.status == "failed"
    assert runtime.calls == []
