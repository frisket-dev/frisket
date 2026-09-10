from __future__ import annotations

from pathlib import Path
from typing import Any

from frisket.authoring.plugin_registry import (
    _reset_default_registry_for_tests,
    default_registry,
    register_trusted_backend_handler,
    unregister_trusted_backend_handler,
)
from frisket.engine.projections.runtime import (
    ProjectionRuntimeError,
    projection_runtime_binding,
    runtime_projection_build,
    runtime_projection_status,
)
from frisket.engine.store import Project
from workbench_runtime_test_helpers import activate_runtime_plugin_for_project


PLUGIN_ID = "frisket-runtime-demo"
PROJECTION_KIND = "frisket.runtime_demo.projection"
HANDLER_KEY = f"{PLUGIN_ID}:projection"


def test_projection_runtime_contract_calls_trusted_status_and_build_handler(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    calls: list[dict[str, Any]] = []

    def trusted_projection_handler(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(payload)
        if payload["schemaVersion"] == "frisket.runtime_projection_status_request.v1":
            return {
                "schemaVersion": "frisket.runtime_projection_status.v1",
                "status": "ready",
                "freshness": {
                    "state": "fresh",
                    "generation": "gen-1",
                    "transient": False,
                },
                "outputs": {
                    "artifactRefs": [
                        {
                            "kind": "projection_artifact",
                            "projectionKind": PROJECTION_KIND,
                            "artifactId": "projection://demo/gen-1",
                        }
                    ],
                    "metrics": {"validRows": 2},
                },
                "warnings": ["status handled by trusted host binding"],
            }
        return {
            "schemaVersion": "frisket.runtime_projection_build_plan.v1",
            "status": "accepted",
            "build": {
                "operation": "refresh",
                "idempotencyKey": "projection-build@sha256:v1",
            },
            "outputs": {
                "artifactRefs": [
                    {
                        "kind": "projection_artifact",
                        "projectionKind": PROJECTION_KIND,
                        "artifactId": "projection://demo/gen-2",
                    }
                ],
                "metrics": {"validRows": 3},
            },
        }

    register_trusted_backend_handler(HANDLER_KEY, trusted_projection_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=HANDLER_KEY,
            handler=trusted_projection_handler,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "runtime-projection.frisket")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"projections": {PROJECTION_KIND: HANDLER_KEY}},
            project_id="runtime-projection-project",
        )

        binding = projection_runtime_binding(PROJECTION_KIND)
        assert binding.kind == PROJECTION_KIND
        assert binding.handler_key == HANDLER_KEY

        status = runtime_projection_status(
            project,
            projection_kind=PROJECTION_KIND,
            project_id="runtime-projection-project",
            target={"sheetId": 7, "columnId": 11},
            params={"bbox": [-74, 40, -73, 41]},
        )
        assert status.schema_version == "frisket.runtime_projection_status.v1"
        assert status.status == "ready"
        assert status.freshness.generation == "gen-1"
        assert status.outputs.metrics == {"validRows": 2}
        assert status.warnings == ["status handled by trusted host binding"]

        build = runtime_projection_build(
            project,
            projection_kind=PROJECTION_KIND,
            project_id="runtime-projection-project",
            target={"sheetId": 7, "columnId": 11},
            params={"bbox": [-74, 40, -73, 41]},
            mode="refresh",
        )
        assert build.schema_version == "frisket.runtime_projection_build_plan.v1"
        assert build.status == "accepted"
        assert build.build.operation == "refresh"
        assert build.build.idempotency_key == "projection-build@sha256:v1"

        assert [call["schemaVersion"] for call in calls] == [
            "frisket.runtime_projection_status_request.v1",
            "frisket.runtime_projection_build_request.v1",
        ]
        assert calls[0]["projectId"] == "runtime-projection-project"
        assert calls[0]["pluginId"] == PLUGIN_ID
        assert calls[0]["handlerKey"] == HANDLER_KEY
        assert calls[0]["projectionKind"] == PROJECTION_KIND
        assert calls[0]["target"] == {"sheetId": 7, "columnId": 11}
        assert calls[0]["params"] == {"bbox": [-74, 40, -73, 41]}
        assert calls[1]["mode"] == "refresh"
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind!='plugin.load'"
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()


def test_projection_runtime_contract_fails_closed_for_unbound_kind(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    project = Project.create(tmp_path / "unbound-projection.frisket")

    assert projection_runtime_binding("frisket.runtime_demo.unbound") is None

    try:
        runtime_projection_status(
            project,
            projection_kind="frisket.runtime_demo.unbound",
            project_id="runtime-projection-project",
            target={"sheetId": 7},
            params={},
        )
    except ProjectionRuntimeError as exc:
        assert exc.code == "unsupported_runtime_projection"
    else:
        raise AssertionError("unbound projection kind should fail closed")


def test_projection_runtime_contract_fails_closed_without_handler(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=HANDLER_KEY,
            handler=None,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "missing-handler-projection.frisket")

        assert projection_runtime_binding(PROJECTION_KIND) is None

        try:
            runtime_projection_build(
                project,
                projection_kind=PROJECTION_KIND,
                project_id="runtime-projection-project",
                target={"sheetId": 7},
                params={},
                mode="refresh",
            )
        except ProjectionRuntimeError as exc:
            assert exc.code == "unsupported_runtime_projection"
        else:
            raise AssertionError("missing projection handler should fail closed")
    finally:
        _reset_default_registry_for_tests()


def test_projection_runtime_contract_rejects_invalid_handler_response(
    tmp_path: Path,
) -> None:
    _reset_default_registry_for_tests()

    def invalid_handler(_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "frisket.runtime_projection_status.v1",
            "status": "ready",
            "outputs": {"rawSql": "SELECT * FROM rows"},
        }

    register_trusted_backend_handler(HANDLER_KEY, invalid_handler)
    try:
        default_registry().register_runtime_binding(
            "projections",
            PROJECTION_KIND,
            handler_key=HANDLER_KEY,
            handler=invalid_handler,
            plugin=PLUGIN_ID,
        )
        project = Project.create(tmp_path / "bad-projection.frisket")
        activate_runtime_plugin_for_project(
            project,
            tmp_path,
            plugin_id=PLUGIN_ID,
            runtime_bindings={"projections": {PROJECTION_KIND: HANDLER_KEY}},
            project_id="runtime-projection-project",
        )

        try:
            runtime_projection_status(
                project,
                projection_kind=PROJECTION_KIND,
                project_id="runtime-projection-project",
                target={"sheetId": 7},
                params={},
            )
        except ProjectionRuntimeError as exc:
            assert exc.code == "invalid_runtime_projection_status"
        else:
            raise AssertionError("invalid projection status should fail closed")

        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM receipts WHERE action_kind!='plugin.load'"
            ).fetchone()[0]
            == 0
        )
        assert project.db.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == 0
    finally:
        unregister_trusted_backend_handler(HANDLER_KEY)
        _reset_default_registry_for_tests()
