from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from frisket.contracts import plugin_rpc as rpc


def _request_payloads() -> list[tuple[type[rpc.PluginRpcModel], dict[str, object]]]:
    common = {
        "pluginId": "example.plugin",
        "handlerKey": "handler",
        "modulePath": "plugin.py",
    }
    return [
        (
            rpc.ImporterRequest,
            {
                **common,
                "importerKind": "example.importer",
                "source": {},
                "context": {
                    "projectId": "project",
                    "pluginId": "example.plugin",
                    "handlerKey": "handler",
                    "importerKind": "example.importer",
                    "capabilities": ["project:write"],
                },
            },
        ),
        (
            rpc.ProjectionRequest,
            {
                **common,
                "projectionKind": "example.projection",
                "context": {
                    "projectId": "project",
                    "pluginId": "example.plugin",
                    "handlerKey": "handler",
                    "projectionKind": "example.projection",
                    "capabilities": [
                        "projection.build",
                        "projection.artifact.write",
                    ],
                },
            },
        ),
        (
            rpc.OperatorRequest,
            {
                **common,
                "operatorKind": "example.operator",
                "context": {
                    "projectId": "project",
                    "pluginId": "example.plugin",
                    "handlerKey": "handler",
                    "operatorKind": "example.operator",
                    "capabilities": ["operator.filter"],
                },
            },
        ),
    ]


@pytest.mark.parametrize(("expected_type", "payload"), _request_payloads())
def test_concrete_request_types_are_closed_and_round_trip(
    expected_type: type[rpc.PluginRpcModel],
    payload: dict[str, object],
) -> None:
    parsed = expected_type.model_validate(payload)
    wire = parsed.model_dump(mode="json", by_alias=True)
    assert expected_type.model_validate(wire) == parsed


@pytest.mark.parametrize(
    ("adapter", "payload", "expected_type"),
    [
        (
            rpc.ImporterFrame,
            {"type": "schema", "columns": []},
            rpc.TableSchemaFrame,
        ),
        (rpc.ImporterFrame, {"type": "row", "row": {}}, rpc.TableRowFrame),
        (
            rpc.ImporterFrame,
            {"type": "diagnostic", "diagnostic": {}},
            rpc.ImporterDiagnosticFrame,
        ),
        (
            rpc.ImporterFrame,
            {"type": "error", "error": {"code": "failed", "message": "safe"}},
            rpc.TableErrorFrame,
        ),
        (
            rpc.ImporterFrame,
            {"type": "done", "row_count": 0},
            rpc.TableDoneFrame,
        ),
        (
            rpc.ImporterFrame,
            {"type": "done", "row_count": 0, "warnings": ["safe"]},
            rpc.TableDoneFrame,
        ),
        (
            rpc.ProjectionFrame,
            {"type": "timeline_item", "item": {}},
            rpc.ProjectionTimelineItemFrame,
        ),
        (
            rpc.ProjectionFrame,
            {"type": "plan", "plan": {}},
            rpc.ProjectionPlanFrame,
        ),
        (
            rpc.ProjectionFrame,
            {"type": "error", "error": {"code": "failed", "message": "safe"}},
            rpc.ProjectionErrorFrame,
        ),
        (
            rpc.ProjectionFrame,
            {"type": "done"},
            rpc.ProjectionDoneFrame,
        ),
        (
            rpc.OperatorResponse,
            {
                "mode": "operator",
                "status": "completed",
                "plan": {"rowIds": []},
            },
            rpc.OperatorResponse,
        ),
    ],
)
def test_frame_and_concrete_response_types_round_trip_per_message(
    adapter: object,
    payload: dict[str, object],
    expected_type: type[rpc.PluginRpcModel],
) -> None:
    type_adapter = TypeAdapter(adapter)
    parsed = type_adapter.validate_python(payload)
    assert isinstance(parsed, expected_type)
    assert (
        type_adapter.validate_python(parsed.model_dump(mode="json", by_alias=True))
        == parsed
    )


@pytest.mark.parametrize(
    "adapter",
    [
        rpc.ImporterFrame,
        rpc.ProjectionFrame,
    ],
)
def test_frame_unions_reject_unknown_type(adapter: object) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(adapter).validate_python({"type": "future_frame"})


def test_internal_rpc_messages_do_not_carry_release_versions() -> None:
    for request_type, payload in _request_payloads():
        parsed = request_type.model_validate(payload)
        assert "schemaVersion" not in parsed.model_dump(mode="json", by_alias=True)
        assert "schemaVersion" not in parsed.context.model_dump(
            mode="json", by_alias=True
        )
    frame = rpc.TableRowFrame(row={})
    assert "schemaVersion" not in frame.model_dump(mode="json", by_alias=True)


def test_secret_snapshot_is_available_to_the_wire_but_hidden_from_repr() -> None:
    payload = _request_payloads()[1][1]
    payload["context"] = {
        **payload["context"],
        "requiresSecrets": ["DEMO_API_KEY"],
        "secretValues": {"DEMO_API_KEY": "stdin-only-secret"},
    }

    parsed = rpc.ProjectionRequest.model_validate(payload)

    assert parsed.context.secret_values == {"DEMO_API_KEY": "stdin-only-secret"}
    assert "stdin-only-secret" not in repr(parsed.context)


def test_protocol_bounds_and_invocation_semantics() -> None:
    # Total work belongs to the caller's machine (or Cloud configuration), not
    # to the trusted-local wire protocol. Per-message framing remains bounded.
    assert not hasattr(rpc, "PLUGIN_PROJECTION_OUTPUT_ROW_CAP")
    assert rpc.PLUGIN_ACTION_WALL_SECONDS == 60
    assert rpc.PLUGIN_INVOCATION_SEMANTICS == rpc.PluginInvocationSemantics(
        wall_seconds=60,
        kill_process_tree_on_cancel=True,
        one_process_per_invocation=True,
    )


def test_structured_error_is_closed_and_excludes_429_compat_shape() -> None:
    assert set(rpc.StructuredPluginError.model_fields) == {
        "code",
        "message",
        "details",
    }
    valid = rpc.StructuredPluginError.model_validate(
        {
            "code": "plugin_provider_rate_limited",
            "message": "safe",
            "details": {"retryable": True, "retry_after_ms": 1000},
        }
    )
    assert valid.details is not None
    assert valid.details.retry_after_ms == 1000
    with pytest.raises(ValidationError):
        rpc.StructuredPluginError.model_validate(
            {"code": "failed", "message": "safe", "secret": "leak"}
        )
    with pytest.raises(ValidationError):
        rpc.StructuredPluginError.model_validate(
            {
                "code": "failed",
                "message": "safe",
                "details": {"status_code": 429},
            }
        )


def test_projection_capability_postures_are_closed() -> None:
    common = {
        "projectId": "project",
        "pluginId": "plugin",
        "handlerKey": "handler",
        "projectionKind": "projection",
    }
    for capabilities in (
        ["projection.build", "projection.artifact.write"],
        ["projection.build"],
        ["projection.status"],
    ):
        rpc.PluginProjectionCapabilityContext.model_validate(
            {**common, "capabilities": capabilities}
        )
    with pytest.raises(ValidationError):
        rpc.PluginProjectionCapabilityContext.model_validate(
            {
                **common,
                "capabilities": ["projection.status", "projection.artifact.write"],
            }
        )


def test_malformed_secret_snapshot_is_not_in_validation_error():
    secret = "invocation-secret-must-not-be-in-diagnostics"
    with pytest.raises(ValidationError) as caught:
        rpc.PluginImporterCapabilityContext.model_validate(
            {
                "projectId": "p",
                "pluginId": "p",
                "handlerKey": "h",
                "importerKind": "csv",
                "capabilities": ["project:write"],
                "secretValues": {"TOKEN": {"wrong-type": secret}},
            }
        )
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
