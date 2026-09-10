from __future__ import annotations

from dataclasses import dataclass

import pytest

from frisket.engine.jobs import HandlerRegistry, JobHandlerContext
from frisket.actions.registry import ACTION_REGISTRY
from frisket.ops.base import Recipe
from frisket.authoring.plugin_registry import (
    PluginRegistry,
    default_registry,
    get_recipe,
    register_recipe,
    unregister_recipe,
)


@dataclass
class ToyRecipe(Recipe):
    consumes_resolution = False  # required declaration (Recipe)
    cost_class = "free"  # required declaration (Recipe)
    name: str = "toy_recipe"
    llm: bool = False
    description: str = "Toy plugin recipe"


def test_default_registry_discovers_builtin_plugin_surfaces():
    registry = default_registry()
    snapshot = registry.to_public()

    recipe_names = {recipe["name"] for recipe in snapshot["recipes"]}
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
        assert registry.get_recipe(kind) is None
        assert ACTION_REGISTRY.get(kind).catalog_entry()["kind"] == kind

    column_type_names = {column["name"] for column in snapshot["column_types"]}
    assert {"text", "json", "geo_point"} <= column_type_names

    importer_names = {importer["name"] for importer in snapshot["importers"]}
    assert {"csv", "xlsx", "pdf", "rss"} <= importer_names

    job_kinds = {job["kind"] for job in snapshot["job_handlers"]}
    assert {"project.run", "source.poll", "enclosure.download"} <= job_kinds


def test_plugin_registry_registers_external_recipe_importer_and_job_handler():
    registry = PluginRegistry()
    recipe = registry.register_recipe(
        ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo"
    )
    assert registry.get_recipe("demo.plugin.toy") is recipe
    assert registry.recipe_specs()[0].plugin == "demo"
    assert registry.recipe_specs()[0].to_public() == {
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


def test_plugin_recipe_refuses_noncanonical_action_kinds():
    registry = PluginRegistry()
    with pytest.raises(ValueError, match="canonical"):
        registry.register_recipe(ToyRecipe(), action_kind="toy_recipe", plugin="demo")


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


def test_native_action_binding_does_not_create_a_second_recipe():
    registry = PluginRegistry()
    binding = registry.register_runtime_binding(
        "actions", "demo.plugin.toy", handler_key="demo:toy"
    )
    assert binding.handler_api == "plugin_typed_action_native"
    assert registry.get_recipe("demo.plugin.toy") is None


@pytest.mark.parametrize("action_kind", ACTION_REGISTRY.action_ids)
@pytest.mark.parametrize("replace", [False, True])
def test_plugin_recipe_cannot_shadow_registered_builtin_action(action_kind, replace):
    registry = PluginRegistry()
    with pytest.raises(ValueError, match="collides with a built-in"):
        registry.register_recipe(
            ToyRecipe(), action_kind=action_kind, plugin="demo", replace=replace
        )
    assert registry.get_recipe(action_kind) is None
    assert registry.recipe_specs() == []


def test_plugin_registry_unload_removes_plugin_owned_recipes():
    registry = PluginRegistry()
    registry.register_recipe(ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo")
    registry.register_importer("demo_import", plugin="demo")
    registry.register_job_handler("demo.job", plugin="demo")

    removed = registry.unload_plugin_entries("demo")

    assert removed["recipes"] == ["demo.plugin.toy"]
    assert registry.get_recipe("demo.plugin.toy") is None
    assert [spec.name for spec in registry.importer_specs()] == []
    assert [spec.kind for spec in registry.job_handler_specs()] == []


def test_global_recipe_registration_seam_is_replace_safe():
    unregister_recipe("demo.plugin.toy")
    try:
        register_recipe(ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo")
        assert isinstance(get_recipe("demo.plugin.toy"), ToyRecipe)
        with pytest.raises(ValueError, match="already registered"):
            register_recipe(ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo")
        register_recipe(
            ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo", replace=True
        )
    finally:
        unregister_recipe("demo.plugin.toy")


@dataclass
class UndeclaredRecipe(Recipe):
    """An out-of-tree recipe that forgot ``consumes_resolution``.

    ``Recipe.consumes_resolution`` is an undefaulted ClassVar precisely so
    "undeclared" is unanswerable rather than a silent "no" — but for a plugin
    that only surfaced as a bare AttributeError from whichever of the six
    consumers asked first, at DISPATCH time, under a run that already exists.
    """

    name: str = "undeclared_recipe"
    llm: bool = False
    description: str = "Forgot the execution-seam declaration"


def test_registering_a_recipe_without_consumes_resolution_is_refused():
    registry = PluginRegistry()
    with pytest.raises(ValueError) as exc_info:
        registry.register_recipe(
            UndeclaredRecipe(), action_kind="demo.plugin.undeclared", plugin="demo"
        )
    message = str(exc_info.value)
    # The refusal NAMES the class, so the plugin author knows what to fix.
    assert "undeclared_recipe" in message
    assert "UndeclaredRecipe" in message
    assert "consumes_resolution" in message
    # Nothing was registered: the refusal is at the boundary, not after.
    assert registry.get_recipe("undeclared_recipe") is None


@dataclass
class UnpricedRecipe(Recipe):
    """An out-of-tree recipe that declared the execution seam but not its cost.

    The same boundary, for the money question. Undeclared used to mean "$0.00
    and no gate": the runner turned every ``estimate() -> None`` into a free
    run, which is under every threshold, so a plugin that spends real money
    launched with no confirmation and nothing complained.
    """

    consumes_resolution = False
    name: str = "unpriced_recipe"
    llm: bool = False
    description: str = "Forgot the cost declaration"


def test_registering_a_recipe_without_cost_class_is_refused():
    registry = PluginRegistry()
    with pytest.raises(ValueError) as exc_info:
        registry.register_recipe(
            UnpricedRecipe(), action_kind="demo.plugin.unpriced", plugin="demo"
        )
    message = str(exc_info.value)
    assert "unpriced_recipe" in message
    assert "UnpricedRecipe" in message
    assert "cost_class" in message
    # The refusal names the three answers, so the author can pick one.
    assert "free" in message and "metered" in message and "unpriceable" in message
    assert registry.get_recipe("unpriced_recipe") is None


def test_a_declared_false_recipe_registers_normally():
    """Control: ``False`` is a declaration, not an omission — the overwhelming
    majority of recipes answer False and must be unaffected."""
    registry = PluginRegistry()
    assert (
        registry.register_recipe(
            ToyRecipe(), action_kind="demo.plugin.toy", plugin="demo"
        )
        is not None
    )
    assert registry.get_recipe("demo.plugin.toy") is not None
