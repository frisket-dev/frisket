from __future__ import annotations

from frisket.authoring.workbench import contracts, plugin_runtime_shared


WORKBENCH_WIRE_SCHEMA_VERSIONS = {
    "FRONTEND_COMPONENT_BINDING_SCHEMA_VERSION": (
        "frisket.workbench_plugin_frontend_component_binding.v1"
    ),
    "PLUGIN_INSTALL_STATE_SCHEMA_VERSION": (
        "frisket.workbench_plugin_install_state.v1"
    ),
    "RUNTIME_INDEX_SCHEMA_VERSION": "frisket.workbench_plugin_runtime_index.v1",
    "RUNTIME_PLUGIN_SCHEMA_VERSION": "frisket.workbench_plugin_runtime_plugin.v1",
}


def test_pure_workbench_contract_owns_runtime_wire_schema_versions() -> None:
    assert {
        name: getattr(contracts, name) for name in WORKBENCH_WIRE_SCHEMA_VERSIONS
    } == WORKBENCH_WIRE_SCHEMA_VERSIONS


def test_runtime_shared_exports_no_wire_schema_version_aliases() -> None:
    assert not {
        name
        for name in WORKBENCH_WIRE_SCHEMA_VERSIONS
        if hasattr(plugin_runtime_shared, name)
    }
