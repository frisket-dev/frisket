from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from frisket.authoring.workbench import (
    plugin_subprocess_importers as importers,
    plugin_subprocess_operators as operators,
    plugin_subprocess_projections as projections,
)
from frisket.engine.store import Project
from frisket.plugins.process_client import PluginProcessError, PluginProcessFailure


@pytest.mark.parametrize(
    ("module", "entrypoint"),
    [
        (importers, importers.run_plugin_importer_subprocess),
        (operators, operators.run_plugin_operator_subprocess),
        (projections, projections.run_plugin_projection_build_subprocess),
    ],
)
def test_each_contribution_type_owns_its_public_lifecycle(
    module: Any,
    entrypoint: Any,
) -> None:
    assert entrypoint.__module__ == module.__name__


def test_process_failure_payload_is_identical_across_contribution_types(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = PluginProcessFailure(
        code="plugin_subprocess_failed",
        message="Trusted-local plugin subprocess failed",
        field="response",
        details={"retryable": True},
    )

    class FailingClient:
        def response_payload(self, response: Any) -> Any:
            return response

        def importer(self, request: Any, env: Any, *, should_cancel=None) -> Any:
            del request, env
            raise PluginProcessError(failure)

        def operator(self, request: Any, env: Any) -> Any:
            del request, env
            raise PluginProcessError(failure)

        def projection(self, request: Any, env: Any) -> Any:
            del request, env
            raise PluginProcessError(failure)

    client = FailingClient()
    for module in (importers, operators, projections):
        monkeypatch.setattr(module, "_plugin_process_client", lambda _root: client)

    common = {
        "pluginId": "example.plugin",
        "handlerKey": "example.plugin:handler",
        "modulePath": "plugin.py",
    }
    project = Project.create(tmp_path / "parity.frisket", name="parity")
    try:
        binding = SimpleNamespace(
            plugin=common["pluginId"],
            handler_key=common["handlerKey"],
            kind="example.plugin.importer",
        )
        importer_response = importers._run_child_importer(
            project,
            plugin_root="plugin-root",
            request={
                **common,
                "importerKind": binding.kind,
                "source": {},
                "handlerParams": {},
                "context": {
                    "projectId": "project",
                    "pluginId": common["pluginId"],
                    "handlerKey": common["handlerKey"],
                    "importerKind": binding.kind,
                    "capabilities": ["project:write"],
                },
            },
            env={},
            binding=binding,
            source_label="source",
        )
    finally:
        project.close()
    operator_response = operators._run_child_operator(
        plugin_root="plugin-root",
        request={
            **common,
            "operatorKind": "example.plugin.operator",
            "target": {},
            "params": {},
            "context": {
                "projectId": "project",
                "pluginId": common["pluginId"],
                "handlerKey": common["handlerKey"],
                "operatorKind": "example.plugin.operator",
                "capabilities": ["operator.filter"],
            },
            "rows": [],
        },
        env={},
    )
    projection_response = projections._run_child_projection(
        plugin_root="plugin-root",
        request={
            **common,
            "projectionKind": "example.plugin.projection",
            "operation": "status",
            "target": {},
            "params": {},
            "context": {
                "projectId": "project",
                "pluginId": common["pluginId"],
                "handlerKey": common["handlerKey"],
                "projectionKind": "example.plugin.projection",
                "capabilities": ["projection.status"],
            },
            "rows": [],
        },
        env={},
    )

    expected = failure.as_dict()
    assert importer_response == expected
    assert operator_response == expected
    assert projection_response == expected
