from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.actions.registry import ACTION_REGISTRY
from frisket.ops.base import ActionProgram
from frisket.authoring.plugin_registry import PluginRegistry, default_registry
from helpers import install_action_program


@dataclass
class ToyProgram(ActionProgram):
    consumes_resolution = False  # required declaration (ActionProgram)
    cost_class = "free"  # required declaration (ActionProgram)
    name: str = "toy_recipe"
    llm: bool = False
    description: str = "Toy plugin recipe"


def test_default_registry_discovers_builtin_plugin_surfaces():
    registry = default_registry()
    snapshot = registry.to_public()

    recipe_names = {program["name"] for program in snapshot["recipes"]}
    assert recipe_names.isdisjoint(ACTION_REGISTRY.action_ids)
    for kind in (
        "map.classify",
        "map.ner",
        "enrich.geocode",
        "enrich.census_demographics",
        "media.ocr",
        "media.transcribe",
    ):
        assert kind not in recipe_names
        assert registry.get_action_program(kind) is None
        assert ACTION_REGISTRY.get(kind).catalog_entry()["kind"] == kind

    column_type_names = {column["name"] for column in snapshot["column_types"]}
    assert {"text", "json", "geo_point"} <= column_type_names

    importer_names = {importer["name"] for importer in snapshot["importers"]}
    assert {"csv", "xlsx", "pdf", "rss"} <= importer_names

    job_kinds = {job["kind"] for job in snapshot["job_handlers"]}
    assert {"project.run", "source.poll", "enclosure.download"} <= job_kinds


def test_plugin_registry_publishes_programs_importers_and_job_handlers():
    registry = PluginRegistry()
    program = install_action_program(
        ToyProgram(), action_kind="demo.plugin.toy", plugin="demo", registry=registry
    )
    assert registry.get_action_program("demo.plugin.toy") is program
    assert registry.action_program_specs()[0].plugin == "demo"
    assert registry.action_program_specs()[0].to_public() == {
        "name": "demo.plugin.toy",
        "version": "1",
        "description": "Toy plugin recipe",
        "llm": False,
        "params": [],
        "plugin": "demo",
    }

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


def test_native_action_binding_does_not_create_a_second_program():
    registry = PluginRegistry()
    binding = registry.register_runtime_binding(
        "actions", "demo.plugin.toy", handler_key="demo:toy"
    )
    assert binding.handler_api == "plugin_typed_action_native"
    assert registry.get_action_program("demo.plugin.toy") is None


def test_plugin_registry_unload_removes_plugin_owned_programs():
    registry = PluginRegistry()
    install_action_program(
        ToyProgram(), action_kind="demo.plugin.toy", plugin="demo", registry=registry
    )
    registry.register_importer("demo_import", plugin="demo")
    registry.register_job_handler("demo.job", plugin="demo")

    removed = registry.unload_plugin_entries("demo")

    assert removed["recipes"] == ["demo.plugin.toy"]
    assert registry.get_action_program("demo.plugin.toy") is None
    assert [spec.name for spec in registry.importer_specs()] == []
    assert [spec.kind for spec in registry.job_handler_specs()] == []
