"""Trusted importer samples bound host collection, not author precomputation."""

from contextlib import closing
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.types import TableError
from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.authoring.workbench.plugin_runtime import (
    activate_workbench_plugin_manifest,
    execute_workbench_plugin_local_install_plan,
)
from frisket.engine.executor import ExecutorDeps, run_action_spec
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.executor.table_preview import preview_table
from frisket.engine.store import Project
from tests.workbench_runtime_test_helpers import write_runtime_plugin_manifest


@pytest.fixture
def importer(tmp_path):
    _reset_default_registry_for_tests()
    instances = []

    def install(*, project=None, count=25, secret=False):
        plugin, kind, key = (
            "preview.importer",
            "preview.importer.read",
            "preview.importer:read",
        )
        calls = []
        plan = {
            "schemaVersion": "frisket.runtime_importer_plan.v1",
            "columns": [{"name": "value", "type": "integer"}],
            "rows": [{"value": n} for n in range(count)],
            "warnings": ["Reported by importer"],
        }

        def handler(payload):
            calls.append(deepcopy(payload))
            return plan

        register_trusted_backend_handler(key, handler)
        default_registry().register_runtime_binding(
            "importers", kind, handler_key=key, handler=handler, plugin=plugin
        )
        path = write_runtime_plugin_manifest(
            tmp_path, plugin_id=plugin, runtime_bindings={"importers": {kind: key}}
        )
        manifest = json.loads(path.read_text())
        if secret:
            manifest["requires"]["secrets"] = ["IMPORTER_TOKEN"]
        path.write_text(json.dumps(manifest))
        if project is None:
            project = Project.create(tmp_path / "project")
            instances.append(project)
        status, payload = execute_workbench_plugin_local_install_plan(
            project,
            project_id="preview",
            plugin_id=plugin,
            source={"kind": "localPath", "value": str(path)},
            arbitrary_package_load_allowed=False,
        )
        assert status == 200, payload
        activate_workbench_plugin_manifest(
            project,
            project_id="preview",
            plugin_id=plugin,
            receipt_id=payload["receiptId"],
            trust_acknowledged=True,
            permissions_accepted=["plugin:trusted_local_backend"],
            arbitrary_package_load_allowed=False,
        )
        request = {
            "action_id": "import.runtime",
            "scope": {"kind": "project"},
            "sheet_name": "Imported",
            "idempotency_key": "import-preview",
            "params": {
                "importer_kind": kind,
                "source": {"kind": "runtime", "label": "fixture"},
            },
        }
        return SimpleNamespace(
            project=project,
            request=request,
            calls=calls,
            plan=plan,
            kind=kind,
            path=path,
        )

    yield install
    for project in instances:
        project.close()
    unregister_trusted_backend_handler("preview.importer:read")
    _reset_default_registry_for_tests()


def sample(imported, *, cancelled=lambda: False):
    request = validate_root_action(imported.request).action
    bound = BoundTypedActionRequest.bind(
        ACTION_REGISTRY.get(request.action_id), request
    )
    return preview_table(
        imported.project,
        "preview",
        bound,
        deps=ExecutorDeps(),
        progress=lambda *_: None,
        cancelled=cancelled,
    )


@pytest.mark.parametrize("count,total", [(0, 0), (3, 3), (20, None), (25, None)])
def test_eager_inline_prefix_and_full_run_parity(importer, count, total):
    imported = importer(count=count)
    before = tuple(imported.project.db.iterdump())
    with closing(sample(imported)) as result:
        assert result.total == total
        assert result.rows == [{"value": {"value": n}} for n in range(min(count, 20))]
        assert result.warnings == ("Reported by importer",)
    assert tuple(imported.project.db.iterdump()) == before
    assert len(imported.calls) == 1
    run = run_action_spec(imported.project, imported.request, project_id="preview")
    assert run.status == "completed", run.errors
    assert (
        imported.project.db.execute("SELECT count(*) FROM rows").fetchone()[0] == count
    )


def test_inline_does_not_validate_n_plus_one_but_run_does(importer):
    imported = importer()
    imported.plan["rows"][20] = {"wrong": "not a valid row"}
    with closing(sample(imported)) as result:
        assert len(result.rows) == 20 and result.total is None
    run = run_action_spec(imported.project, imported.request, project_id="preview")
    assert run.status == "failed"
    assert imported.project.sheets(include_hidden=True) == []


def test_secret_requirement_refuses_before_native_handler(importer):
    imported = importer(secret=True)
    before = tuple(imported.project.db.iterdump())
    with pytest.raises(TableReadRefused) as error:
        sample(imported)
    assert error.value.error.code == "preview_effect_requires_run"
    assert imported.calls == []
    assert tuple(imported.project.db.iterdump()) == before


def test_revoked_project_grant_refuses_before_handler(importer):
    imported = importer()
    imported.project.db.execute(
        "UPDATE workbench_plugin_installs SET permissions_accepted='[]'"
    )
    imported.project.db.commit()
    with pytest.raises(TableError, match="No trusted importer"):
        sample(imported)
    assert imported.calls == []


def test_dispatch_does_not_turn_required_metadata_into_grants(importer):
    imported = importer()
    binding = next(iter(default_registry().runtime_binding_specs("importers")))
    binding.metadata["required_capabilities"] = ["external:paid_service"]
    with pytest.raises(TableReadRefused) as error:
        sample(imported)
    assert error.value.error.code == "plugin_capability_required"
    assert imported.calls == []


def test_cancel_before_and_after_inline_call(importer):
    imported = importer()
    with pytest.raises(TableError, match="cancelled"):
        sample(imported, cancelled=lambda: True)
    assert imported.calls == []
    with pytest.raises(TableError, match="cancelled"):
        sample(imported, cancelled=lambda: bool(imported.calls))
    assert len(imported.calls) == 1
    assert imported.project.sheets(include_hidden=True) == []
