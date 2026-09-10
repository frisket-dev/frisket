"""Catalog membership comes from typed definitions without broadening plugins."""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import root_action_catalog_payload, validate_root_action
from frisket.authoring import plugin_registry
from frisket.contracts.action import ActionSpec
from frisket.contracts.actions.runtime import validate_plugin_manifest_contributions
from frisket.contracts.plugin import PluginManifest
from frisket.operability.telemetry import ProductTelemetryEvent


@pytest.mark.parametrize(
    "first",
    [
        "frisket.contracts.action",
        "frisket.contracts.action_validation",
        "frisket.operability.telemetry",
        "frisket.authoring.workbench.plugin_runtime_capabilities",
        "frisket.actions.registry",
    ],
)
def test_catalog_consumers_import_cold(first: str) -> None:
    code = (
        f"import {first}; "
        "from frisket.actions.system import root_action_catalog; "
        "from frisket.actions.registry import ACTION_REGISTRY; "
        "assert {entry.kind for entry in root_action_catalog().actions} "
        "== set(ACTION_REGISTRY.action_ids)"
    )
    result = subprocess.run(
        [
            sys.executable,  # subprocess-boundary: exercise fresh-interpreter imports
            "-c",
            code,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_root_and_served_catalog_keep_exact_schema_and_builtin_membership() -> None:
    from frisket.server.action_catalog_hints import (
        action_catalog_payload_with_launcher_hints,
    )

    root = root_action_catalog_payload()
    served = action_catalog_payload_with_launcher_hints({})
    for key in root.keys() - {"actions"}:
        assert served[key] == root[key]
    assert [entry["kind"] for entry in served["actions"]] == [
        entry["kind"] for entry in root["actions"]
    ]
    assert {entry["kind"] for entry in root["actions"]} == set(
        ACTION_REGISTRY.action_ids
    )
    assert "action_id" in root["action_schema"]["properties"]
    assert "kind" not in root["action_schema"]["properties"]
    assert "ActionSpec" not in root["action_schema"]["$defs"]


@pytest.fixture
def runtime_registry(monkeypatch):
    registry = plugin_registry.PluginRegistry()
    monkeypatch.setattr(plugin_registry, "default_registry", lambda: registry)
    return registry


def test_legacy_runtime_envelope_cannot_reenter_the_native_root(
    runtime_registry,
) -> None:
    request = {
        "schema_version": "frisket.action.v2",
        "kind": "example.clean",
        "params": {"explicit_null": None, "value": "original"},
    }
    assert validate_root_action(request).error.code == "invalid_action_request"
    runtime_registry.register_runtime_binding(
        "actions", "example.clean", handler_key="example:clean"
    )
    assert validate_root_action(request).error.code == "invalid_action_request"
    runtime_registry.register_runtime_binding(
        "actions",
        "example.clean",
        handler_key="example:clean",
        handler=lambda *_: None,
        replace=True,
    )
    assert validate_root_action(request).error.code == "invalid_action_request"
    assert validate_root_action({**request, "params": []}).error.code == (
        "invalid_action_request"
    )


def test_runtime_binding_cannot_reenable_builtin_old_envelope(runtime_registry) -> None:
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        _validate_public_action_collision,
    )
    from frisket.authoring.workbench.plugin_runtime_shared import (
        WorkbenchPluginActivationError,
    )

    for kind in ACTION_REGISTRY.action_ids:
        runtime_registry.register_runtime_binding(
            "actions",
            kind,
            handler_key="example:shadow",
            handler=lambda *_: None,
        )
        refusal = validate_root_action(
            {
                "schema_version": "frisket.action.v2",
                "kind": kind,
                "params": {},
            }
        )
        assert not refusal.ok
        assert refusal.error.code == "invalid_action_request"
        with pytest.raises(WorkbenchPluginActivationError):
            _validate_public_action_collision(SimpleNamespace(kind=kind))


@pytest.mark.parametrize(
    ("kind", "capabilities", "accepted"),
    [
        ("map.ner", [], True),
        ("example.clean", [], False),
        ("example.clean", ["plugin:trusted_local_backend"], True),
        ("example.clean", ["external:arbitrary"], False),
    ],
)
def test_manifest_membership_keeps_trust_capability_boundary(
    kind, capabilities, accepted
) -> None:
    manifest = PluginManifest(
        id="example.plugin",
        version="1.0.0",
        contributes={"actions": [kind]},
        requires={"capabilities": capabilities},
    )
    result = validate_plugin_manifest_contributions(
        ActionSpec(kind="plugin.load", params={}),
        SimpleNamespace(manifest=manifest),
    )
    assert (result is None) is accepted
    if result is not None:
        assert result.error.code == "invalid_plugin_manifest"


def test_telemetry_accepts_only_builtin_ids_or_coarse_labels(runtime_registry) -> None:
    runtime_registry.register_runtime_binding(
        "actions",
        "example.clean",
        handler_key="example:clean",
        handler=lambda *_: None,
    )
    for kind in (*ACTION_REGISTRY.action_ids, "plugin", "other"):
        ProductTelemetryEvent("Action.opened", {"action": kind})
    for kind in ("example.clean", "research.project_answer", " map.ner", "", False):
        with pytest.raises(ValueError, match="unregistered product telemetry value"):
            ProductTelemetryEvent("Action.opened", {"action": kind})


@pytest.mark.parametrize("kind", ["map.ner", "example.clean"])
def test_mcp_does_not_enable_old_envelopes(kind: str) -> None:
    from frisket.server.mcp.backends import _action_payload

    with pytest.raises(ValueError, match="unknown canonical action kind"):
        _action_payload(
            {"schema_version": "frisket.action.v2", "kind": kind, "params": {}},
            confirmed=True,
            consented_promise_set_hash="cannot-enable-old-envelope",
        )


def test_legacy_plugin_catalog_requires_its_own_errors() -> None:
    from dataclasses import replace
    from pydantic import BaseModel
    from frisket.contracts.action import (
        ActionErrorSpec,
        CostPolicy,
        IdempotencyPolicy,
        RetryPolicy,
    )
    from frisket.sdk.catalog import build_catalog
    from frisket.sdk.declaration import Op

    class Params(BaseModel):
        value: str

    declared_error = ActionErrorSpec(code="plugin_error", message="Plugin error.")
    declaration = Op(
        kind="example.clean",
        title="Clean",
        description="Clean text.",
        params_model=Params,
        output_model=Params,
        errors=(declared_error,),
        side_effects=(),
        cost=CostPolicy(kind="none"),
        idempotency=IdempotencyPolicy(
            supported=False,
            scope="project",
            key_field="idempotency_key",
            behavior="Not supported.",
        ),
        retry=RetryPolicy(supported=False, strategy="none"),
        examples=(),
        primary_fields=(),
    )
    assert build_catalog(declaration).errors == [declared_error]
    for kind in ("example.clean", "map.ner"):
        with pytest.raises(LookupError, match="errors must be declared"):
            build_catalog(replace(declaration, kind=kind, errors=None))
