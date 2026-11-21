from __future__ import annotations

import pytest

from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.authoring.plugin_registry import PluginRegistry, default_registry


def test_default_registry_discovers_builtin_plugin_surfaces():
    registry = default_registry()
    snapshot = registry.to_public()

    assert "recipes" not in snapshot

    column_type_names = {column["name"] for column in snapshot["column_types"]}
    assert {"text", "json", "geo_point"} <= column_type_names

    importer_names = {importer["name"] for importer in snapshot["importers"]}
    assert {"csv", "xlsx", "pdf", "rss"} <= importer_names

    job_kinds = {job["kind"] for job in snapshot["job_handlers"]}
    assert {"project.run", "feed.poll", "enclosure.download"} <= job_kinds


def test_plugin_registry_publishes_importers_and_job_handlers():
    registry = PluginRegistry()

    importer = registry.register_importer(
        "ndjson",
        extensions=[".ndjson"],
        media_types=["application/x-ndjson"],
        description="Import newline-delimited JSON.",
        plugin="demo",
    )
    assert importer.to_public() == {
        "name": "ndjson",
        "extensions": [".ndjson"],
        "media_types": ["application/x-ndjson"],
        "description": "Import newline-delimited JSON.",
        "plugin": "demo",
        "has_handler": False,
    }

    def handler(job):
        return {"handled": job}

    registry.register_job_handler("demo.job", handler, plugin="demo")
    handlers = HandlerRegistry()
    registry.install_job_handlers(handlers)
    installed = handlers.get("demo.job")
    assert installed is not None
    assert installed({"row": 7}, JobHandlerContext.without_job_row()) == {
        "handled": {"row": 7}
    }


@pytest.mark.parametrize(
    "handler_api", ["plugin_action", "plugin_action_subprocess", "envelope"]
)
def test_runtime_action_binding_requires_native_handler(handler_api):
    registry = PluginRegistry()
    with pytest.raises(ValueError, match="handler_api"):
        registry.register_runtime_binding(
            "actions",
            "demo.plugin.toy",
            handler_key="demo:toy",
            handler_api=handler_api,
        )
    assert registry.runtime_binding_specs("actions") == []


def test_native_action_binding_uses_native_handler_api():
    registry = PluginRegistry()
    binding = registry.register_runtime_binding(
        "actions", "demo.plugin.toy", handler_key="demo:toy"
    )
    assert binding.handler_api == "plugin_typed_action_native"
    assert [spec.kind for spec in registry.runtime_binding_specs("actions")] == [
        "demo.plugin.toy"
    ]


def test_plugin_registry_unload_removes_plugin_owned_entries():
    registry = PluginRegistry()
    registry.register_importer("demo_import", plugin="demo")
    registry.register_job_handler("demo.job", plugin="demo")

    removed = registry.unload_plugin_entries("demo")

    assert "recipes" not in removed
    assert removed["importers"] == ["demo_import"]
    assert removed["jobHandlers"] == ["demo.job"]
    assert [spec.name for spec in registry.importer_specs()] == []
    assert [spec.kind for spec in registry.job_handler_specs()] == []
