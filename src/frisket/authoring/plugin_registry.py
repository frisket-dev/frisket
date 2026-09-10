"""Narrow plugin registry seams for recipes, column types, importers, and jobs.

Existing runtime owners still do the real work: recipes are ``Recipe`` objects,
column types are registered in ``frisket.column_types``, and job handlers
install into the queue worker's ``HandlerRegistry``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from frisket.authoring import column_types
from frisket.ops.enclosures import ENCLOSURE_DOWNLOAD_KIND
from frisket.contracts.plugin import PLUGIN_ID_RE, LoadedPluginManifest
from frisket.engine.jobs import (
    RUN_PROJECT_KIND,
    SOURCE_POLL_KIND,
    HandlerRegistry,
    JobHandlerContext,
)
from frisket.engine.jobs.worker import ECHO_KIND
from frisket.ops.base import Recipe

# Sentinel for "the class never declared it" — distinct from a declared
# False, which is the overwhelmingly common (and correct) answer.
_UNDECLARED = object()
PluginJobHandler = Callable[[dict], object]


@dataclass(frozen=True)
class ImporterSpec:
    name: str
    extensions: tuple[str, ...] = ()
    media_types: tuple[str, ...] = ()
    description: str = ""
    plugin: str = "core"
    handler: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "extensions": list(self.extensions),
            "media_types": list(self.media_types),
            "description": self.description,
            "plugin": self.plugin,
            "has_handler": self.handler is not None,
        }


@dataclass(frozen=True)
class RecipeSpec:
    action_kind: str
    recipe: Recipe
    plugin: str = "core"

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.action_kind,
            "version": self.recipe.version,
            "description": self.recipe.description,
            "llm": self.recipe.llm,
            "params": self.recipe.params(),
            "plugin": self.plugin,
        }


@dataclass(frozen=True)
class JobHandlerSpec:
    kind: str
    handler: PluginJobHandler | None = field(default=None, repr=False, compare=False)
    description: str = ""
    plugin: str = "core"

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "description": self.description,
            "plugin": self.plugin,
            "has_handler": self.handler is not None,
        }


@dataclass(frozen=True)
class RuntimeBindingSpec:
    kind: str
    handler_key: str
    plugin: str
    binding_type: str
    handler_api: str
    metadata: dict[str, Any] = field(default_factory=dict, compare=False)
    handler: Callable[..., object] | None = field(
        default=None, repr=False, compare=False
    )

    def to_public(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "handler_key": self.handler_key,
            "plugin": self.plugin,
            "binding_type": self.binding_type,
            "handler_api": self.handler_api,
            "has_handler": self.handler is not None,
            "metadata": self.metadata,
        }


class PluginRegistry:
    """Concrete registry object plugins can populate in tests or app startup."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._recipes: dict[str, RecipeSpec] = {}
        self._importers: dict[str, ImporterSpec] = {}
        self._job_handlers: dict[str, JobHandlerSpec] = {}
        self._plugin_manifests: dict[str, LoadedPluginManifest] = {}
        self._runtime_bindings: dict[str, dict[str, RuntimeBindingSpec]] = {
            "actions": {},
            "importers": {},
            "operators": {},
            "projections": {},
        }

    def register_plugin_manifest(
        self, loaded: LoadedPluginManifest, *, replace: bool = False
    ) -> LoadedPluginManifest:
        plugin_id = loaded.manifest.id
        if not PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ValueError("plugin manifest id must be a canonical plugin id")
        with self._lock:
            if plugin_id in self._plugin_manifests and not replace:
                raise ValueError(f"plugin manifest already registered: {plugin_id}")
            self._plugin_manifests[plugin_id] = loaded
        return loaded

    def unregister_plugin_manifest(self, plugin_id: str) -> None:
        with self._lock:
            self._plugin_manifests.pop(plugin_id, None)

    def _restore_plugin_manifest(
        self,
        plugin_id: str,
        previous: LoadedPluginManifest | None,
        *,
        expected: LoadedPluginManifest,
    ) -> None:
        with self._lock:
            current = self._plugin_manifests.get(plugin_id)
            if current is previous:
                return
            if current is not expected:
                raise RuntimeError(
                    f"plugin manifest changed while restoring activation: {plugin_id}"
                )
            if previous is None:
                self._plugin_manifests.pop(plugin_id, None)
            else:
                self._plugin_manifests[plugin_id] = previous

    def plugin_manifests(self) -> list[LoadedPluginManifest]:
        with self._lock:
            return [
                self._plugin_manifests[plugin_id]
                for plugin_id in sorted(self._plugin_manifests)
            ]

    def register_recipe(
        self,
        recipe: Recipe,
        *,
        action_kind: str,
        plugin: str = "external",
        replace: bool = False,
    ) -> Recipe:
        if action_kind != action_kind.strip() or "." not in action_kind:
            raise ValueError("recipe action_kind must be canonical")
        if plugin != "core":
            from frisket.actions.registry import ACTION_REGISTRY

            if action_kind in ACTION_REGISTRY.action_ids:
                raise ValueError(
                    f"plugin action kind collides with a built-in: {action_kind}"
                )
        if not recipe.name:
            raise ValueError("recipe name must be non-empty")
        # Close the declaration at the REGISTRATION boundary (E-3's other
        # half). ``Recipe.consumes_resolution`` is an undefaulted ClassVar so
        # that "undeclared" is unanswerable rather than a silent "no" — but
        # for an out-of-tree recipe that only surfaced as a bare
        # AttributeError from whichever of the six consumers happened to ask
        # first, at DISPATCH time, under a run that already exists. In-tree
        # recipes are covered by a declaration test; plugins are covered
        # here, where the refusal can still name the class.
        if getattr(type(recipe), "consumes_resolution", _UNDECLARED) is _UNDECLARED:
            raise ValueError(
                f"recipe {recipe.name!r} ({type(recipe).__module__}."
                f"{type(recipe).__qualname__}) does not declare "
                "'consumes_resolution'; every recipe must state whether its "
                "runs compose with the execution seam (resolve a route, "
                "compile promises, gate on claims, verify at dispatch). "
                "Declare 'consumes_resolution = False' unless the recipe "
                "consumes a resolved execution route."
            )
        # Same boundary, same reason, for the money question: an undeclared
        # cost class used to be a silent "$0.00 and no gate" for any recipe
        # whose estimate() returned None.
        if getattr(type(recipe), "cost_class", _UNDECLARED) is _UNDECLARED:
            raise ValueError(
                f"recipe {recipe.name!r} ({type(recipe).__module__}."
                f"{type(recipe).__qualname__}) does not declare 'cost_class'; "
                "every recipe must state what an ABSENT estimate means for it "
                "— 'free' (no money moves), 'metered' (a priced meter exists, "
                "so a missing number is missing, not zero), or 'unpriceable' "
                "(the price cannot be known before the run, so it needs an "
                "explicit unknown-cost confirmation)."
            )
        with self._lock:
            if action_kind in self._recipes and not replace:
                raise ValueError(f"action kind already registered: {action_kind}")
            self._recipes[action_kind] = RecipeSpec(
                action_kind=action_kind, recipe=recipe, plugin=plugin
            )
        return recipe

    def unregister_recipe(self, name: str) -> None:
        with self._lock:
            self._recipes.pop(name, None)

    def get_recipe(self, name: str) -> Recipe | None:
        with self._lock:
            spec = self._recipes.get(name)
            return spec.recipe if spec is not None else None

    def recipe_specs(self) -> list[RecipeSpec]:
        with self._lock:
            return [self._recipes[name] for name in sorted(self._recipes)]

    def register_column_type(
        self,
        name: str,
        *,
        validate: Callable[[Any], bool] | None = None,
        parse: Callable[[Any], Any] | None = None,
        presentation: dict[str, Any] | None = None,
        description: str = "",
        plugin: str = "external",
    ) -> column_types.ColumnTypeSpec:
        """Register a process-global plugin column type.

        Column type validation is owned by ``frisket.column_types`` so the
        store sees the same type palette regardless of which registry instance
        performed registration.
        """
        if column_types.get_column_type(name) is not None:
            raise ValueError(f"column type already registered: {name}")
        return column_types.register_column_type(
            name,
            validate=validate,
            parse=parse,
            presentation=presentation,
            description=description,
            core=False,
            plugin=plugin,
        )

    def column_type_specs(self) -> list[column_types.ColumnTypeSpec]:
        return column_types.column_types()

    def register_importer(
        self,
        name: str,
        *,
        extensions: list[str] | tuple[str, ...] = (),
        media_types: list[str] | tuple[str, ...] = (),
        description: str = "",
        plugin: str = "external",
        handler: Callable[..., Any] | None = None,
        replace: bool = False,
    ) -> ImporterSpec:
        if not name:
            raise ValueError("importer name must be non-empty")
        spec = ImporterSpec(
            name=name,
            extensions=tuple(extensions),
            media_types=tuple(media_types),
            description=description,
            plugin=plugin,
            handler=handler,
        )
        with self._lock:
            if name in self._importers and not replace:
                raise ValueError(f"importer already registered: {name}")
            self._importers[name] = spec
        return spec

    def importer_specs(self) -> list[ImporterSpec]:
        with self._lock:
            return [self._importers[name] for name in sorted(self._importers)]

    def unregister_importer(self, name: str) -> None:
        with self._lock:
            self._importers.pop(name, None)

    def register_job_handler(
        self,
        kind: str,
        handler: PluginJobHandler | None = None,
        *,
        description: str = "",
        plugin: str = "external",
        replace: bool = False,
    ) -> JobHandlerSpec:
        if not kind:
            raise ValueError("job handler kind must be non-empty")
        spec = JobHandlerSpec(
            kind=kind,
            handler=handler,
            description=description,
            plugin=plugin,
        )
        with self._lock:
            if kind in self._job_handlers and not replace:
                raise ValueError(f"job handler already registered: {kind}")
            self._job_handlers[kind] = spec
        return spec

    def job_handler_specs(self) -> list[JobHandlerSpec]:
        with self._lock:
            return [self._job_handlers[kind] for kind in sorted(self._job_handlers)]

    def unregister_job_handler(self, kind: str) -> None:
        with self._lock:
            self._job_handlers.pop(kind, None)

    def _restore_job_handler(
        self,
        kind: str,
        previous: JobHandlerSpec | None,
        *,
        expected_plugin: str,
    ) -> None:
        with self._lock:
            current = self._job_handlers.get(kind)
            if current is previous:
                return
            if current is None or current.plugin != expected_plugin:
                raise RuntimeError(
                    f"job handler changed while restoring activation: {kind}"
                )
            if previous is None:
                self._job_handlers.pop(kind, None)
            else:
                self._job_handlers[kind] = previous

    def register_runtime_binding(
        self,
        binding_type: str,
        kind: str,
        *,
        handler_key: str,
        handler: Callable[..., object] | None = None,
        handler_api: str | None = None,
        metadata: dict[str, Any] | None = None,
        plugin: str = "external",
        replace: bool = False,
    ) -> RuntimeBindingSpec:
        if binding_type not in self._runtime_bindings:
            raise ValueError(f"unknown runtime binding type: {binding_type}")
        if not kind:
            raise ValueError("runtime binding kind must be non-empty")
        if not handler_key:
            raise ValueError("runtime binding handler key must be non-empty")
        effective_handler_api = (
            handler_api
            if handler_api is not None
            else (
                "plugin_typed_action_native"
                if binding_type == "actions"
                else "envelope"
            )
        )
        if effective_handler_api not in {
            "envelope",
            "plugin_typed_action_native",
            "plugin_importer_subprocess",
            "plugin_operator_subprocess",
            "plugin_projection_subprocess",
        }:
            raise ValueError(
                "runtime binding handler_api must be envelope, plugin_typed_action_native, "
                "plugin_importer_subprocess, "
                "plugin_operator_subprocess, or plugin_projection_subprocess"
            )
        if (
            binding_type == "actions"
            and effective_handler_api != "plugin_typed_action_native"
        ):
            raise ValueError(
                "action runtime bindings must use plugin_typed_action_native handler_api"
            )
        if binding_type == "importers" and effective_handler_api not in {
            "envelope",
            "plugin_importer_subprocess",
        }:
            raise ValueError(
                "importer runtime bindings must use envelope or "
                "plugin_importer_subprocess handler_api"
            )
        if binding_type == "projections" and effective_handler_api not in {
            "envelope",
            "plugin_projection_subprocess",
        }:
            raise ValueError(
                "projection runtime bindings must use envelope or "
                "plugin_projection_subprocess handler_api"
            )
        if binding_type == "operators" and effective_handler_api not in {
            "envelope",
            "plugin_operator_subprocess",
        }:
            raise ValueError(
                "operator runtime bindings must use envelope or "
                "plugin_operator_subprocess handler_api"
            )
        if (
            binding_type not in {"actions", "importers", "operators", "projections"}
            and effective_handler_api != "envelope"
        ):
            raise ValueError(
                "plugin action/importer/operator/projection handler_api values are only "
                "valid for matching runtime bindings"
            )
        spec = RuntimeBindingSpec(
            kind=kind,
            handler_key=handler_key,
            plugin=plugin,
            binding_type=binding_type,
            handler=handler,
            handler_api=effective_handler_api,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            bindings = self._runtime_bindings[binding_type]
            if kind in bindings and not replace:
                raise ValueError(f"runtime binding already registered: {kind}")
            bindings[kind] = spec
        return spec

    def runtime_binding_specs(self, binding_type: str) -> list[RuntimeBindingSpec]:
        if binding_type not in self._runtime_bindings:
            raise ValueError(f"unknown runtime binding type: {binding_type}")
        with self._lock:
            bindings = self._runtime_bindings[binding_type]
            return [bindings[kind] for kind in sorted(bindings)]

    def _restore_runtime_binding(
        self,
        binding_type: str,
        kind: str,
        previous: RuntimeBindingSpec | None,
        *,
        expected_plugin: str,
    ) -> None:
        if binding_type not in self._runtime_bindings:
            raise ValueError(f"unknown runtime binding type: {binding_type}")
        with self._lock:
            bindings = self._runtime_bindings[binding_type]
            current = bindings.get(kind)
            if current is previous:
                return
            if current is None or current.plugin != expected_plugin:
                raise RuntimeError(
                    "runtime binding changed while restoring activation: "
                    f"{binding_type}/{kind}"
                )
            if previous is None:
                bindings.pop(kind, None)
            else:
                bindings[kind] = previous

    def install_job_handlers(self, registry: HandlerRegistry) -> HandlerRegistry:
        with self._lock:
            specs = list(self._job_handlers.values())
        for spec in specs:
            if spec.handler is not None:
                plugin_handler = spec.handler

                def installed(
                    payload: dict,
                    _context: JobHandlerContext,
                    *,
                    _handler=plugin_handler,
                ) -> object:
                    return _handler(payload)

                registry.register(spec.kind, installed)
        return registry

    def unload_plugin_contributions(self, plugin_id: str) -> None:
        """Remove contributions for package replacement, preserving identity."""
        with self._lock:
            self._importers = {
                name: spec
                for name, spec in self._importers.items()
                if spec.plugin != plugin_id
            }
            self._job_handlers = {
                kind: spec
                for kind, spec in self._job_handlers.items()
                if spec.plugin != plugin_id
            }
            for binding_type, bindings in self._runtime_bindings.items():
                self._runtime_bindings[binding_type] = {
                    kind: spec
                    for kind, spec in bindings.items()
                    if spec.plugin != plugin_id
                }
            column_types.unregister_plugin_column_types(plugin_id)
            from frisket.features.url_classification.plugin_matchers import (
                unregister_plugin_matchers,
            )

            unregister_plugin_matchers(plugin_id)

    def unload_plugin_entries(self, plugin_id: str) -> dict[str, list[str]]:
        """Remove process-local entries owned by one plugin id."""
        removed_manifests: list[str] = []
        removed_recipes: list[str] = []
        removed_importers: list[str] = []
        removed_job_handlers: list[str] = []
        removed_runtime_bindings: dict[str, list[str]] = {
            "actions": [],
            "importers": [],
            "operators": [],
            "projections": [],
        }
        with self._lock:
            # Low-level, unconditional teardown seam. Under the
            # single-workspace-authority model project disable does not call
            # this (the shared package stays registered); workspace uninstall
            # uses it after deleting the catalog authority.
            if self._plugin_manifests.pop(plugin_id, None) is not None:
                removed_manifests.append(plugin_id)
            recipe_names = [
                name for name, spec in self._recipes.items() if spec.plugin == plugin_id
            ]
            importer_names = [
                name
                for name, spec in self._importers.items()
                if spec.plugin == plugin_id
            ]
            job_handler_kinds = [
                kind
                for kind, spec in self._job_handlers.items()
                if spec.plugin == plugin_id
            ]
            for name in recipe_names:
                self._recipes.pop(name, None)
                removed_recipes.append(name)
            for name in importer_names:
                self._importers.pop(name, None)
                removed_importers.append(name)
            for kind in job_handler_kinds:
                self._job_handlers.pop(kind, None)
                removed_job_handlers.append(kind)
            for binding_type, bindings in self._runtime_bindings.items():
                binding_kinds = [
                    kind for kind, spec in bindings.items() if spec.plugin == plugin_id
                ]
                for kind in binding_kinds:
                    bindings.pop(kind, None)
                    removed_runtime_bindings[binding_type].append(kind)
            removed_column_types = column_types.unregister_plugin_column_types(
                plugin_id
            )
            from frisket.features.url_classification.plugin_matchers import (
                unregister_plugin_matchers,
            )

            removed_matchers = unregister_plugin_matchers(plugin_id)
        return {
            "manifests": sorted(removed_manifests),
            "recipes": sorted(removed_recipes),
            "columnTypes": sorted(removed_column_types),
            "matchers": sorted(removed_matchers),
            "importers": sorted(removed_importers),
            "jobHandlers": sorted(removed_job_handlers),
            "runtimeBindings": {
                key: sorted(values) for key, values in removed_runtime_bindings.items()
            },
        }

    def to_public(self) -> dict[str, Any]:
        return {
            "plugin_manifests": [
                {
                    "id": loaded.manifest.id,
                    "version": loaded.manifest.version,
                    "sha256": loaded.sha256,
                    "byte_count": loaded.byte_count,
                    "contributes": loaded.manifest.contributes.model_dump(mode="json"),
                }
                for loaded in self.plugin_manifests()
            ],
            "recipes": [spec.to_public() for spec in self.recipe_specs()],
            "column_types": [spec.to_public() for spec in self.column_type_specs()],
            "importers": [spec.to_public() for spec in self.importer_specs()],
            "job_handlers": [spec.to_public() for spec in self.job_handler_specs()],
            "runtime_bindings": {
                key: [spec.to_public() for spec in self.runtime_binding_specs(key)]
                for key in sorted(self._runtime_bindings)
            },
        }


_DEFAULT: PluginRegistry | None = None
_DEFAULT_LOCK = threading.Lock()
_TRUSTED_BACKEND_HANDLERS: dict[str, PluginJobHandler] = {}
_TRUSTED_BACKEND_HANDLERS_LOCK = threading.RLock()


def _register_builtin_importers(registry: PluginRegistry) -> None:
    for name, extensions, media_types, description in (
        ("csv", (".csv",), ("text/csv",), "Import comma-separated values."),
        (
            "xlsx",
            (".xlsx",),
            ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",),
            "Import Excel workbooks.",
        ),
        (
            "ndjson",
            (".ndjson", ".jsonl"),
            ("application/x-ndjson", "application/jsonl"),
            "Import newline-delimited JSON objects.",
        ),
        (
            "geojson",
            (".geojson", ".json"),
            ("application/geo+json", "application/json"),
            "Import GeoJSON FeatureCollections.",
        ),
        (
            "kml",
            (".kml", ".kmz"),
            (
                "application/vnd.google-earth.kml+xml",
                "application/vnd.google-earth.kmz",
            ),
            "Import KML/KMZ placemarks.",
        ),
        ("pdf", (".pdf",), ("application/pdf",), "Import PDF pages/files."),
        ("files", (), ("*/*",), "Import uploaded files as media cells."),
        ("urls", (), ("text/uri-list",), "Import remote media URLs."),
        ("rss", (".rss", ".xml"), ("application/rss+xml",), "Poll RSS feeds."),
    ):
        registry.register_importer(
            name,
            extensions=extensions,
            media_types=media_types,
            description=description,
            plugin="core",
            replace=True,
        )


def _register_builtin_job_handlers(registry: PluginRegistry) -> None:
    for kind, description in (
        (ECHO_KIND, "Test echo handler."),
        (RUN_PROJECT_KIND, "Execute a prepared project recipe run."),
        (SOURCE_POLL_KIND, "Poll a scheduled project source."),
        (ENCLOSURE_DOWNLOAD_KIND, "Download RSS/media enclosures."),
    ):
        registry.register_job_handler(
            kind,
            description=description,
            plugin="core",
            replace=True,
        )


def default_registry() -> PluginRegistry:
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                registry = PluginRegistry()
                _register_builtin_importers(registry)
                _register_builtin_job_handlers(registry)
                _DEFAULT = registry
    return _DEFAULT


def _reset_default_registry_for_tests() -> None:
    global _DEFAULT
    with _DEFAULT_LOCK:
        _DEFAULT = None
    with _TRUSTED_BACKEND_HANDLERS_LOCK:
        _TRUSTED_BACKEND_HANDLERS.clear()


def register_trusted_backend_handler(
    key: str, handler: PluginJobHandler, *, replace: bool = False
) -> PluginJobHandler:
    if not key:
        raise ValueError("trusted backend handler key must be non-empty")
    plugin_id, separator, _handler_name = key.partition(":")
    if not separator:
        raise ValueError(
            "trusted backend handler key must be namespaced as '<plugin_id>:<key>'"
        )
    if not PLUGIN_ID_RE.fullmatch(plugin_id):
        raise ValueError(
            "trusted backend handler key prefix must be a canonical plugin id"
        )
    with _TRUSTED_BACKEND_HANDLERS_LOCK:
        if key in _TRUSTED_BACKEND_HANDLERS and not replace:
            raise ValueError(f"trusted backend handler already registered: {key}")
        _TRUSTED_BACKEND_HANDLERS[key] = handler
    return handler


def unregister_trusted_backend_handler(key: str) -> None:
    with _TRUSTED_BACKEND_HANDLERS_LOCK:
        _TRUSTED_BACKEND_HANDLERS.pop(key, None)


def get_trusted_backend_handler(key: str) -> PluginJobHandler | None:
    with _TRUSTED_BACKEND_HANDLERS_LOCK:
        return _TRUSTED_BACKEND_HANDLERS.get(key)


def register_recipe(
    recipe: Recipe,
    *,
    action_kind: str,
    plugin: str = "external",
    replace: bool = False,
) -> Recipe:
    return default_registry().register_recipe(
        recipe, action_kind=action_kind, plugin=plugin, replace=replace
    )


def unregister_recipe(name: str) -> None:
    default_registry().unregister_recipe(name)


def get_recipe(name: str) -> Recipe | None:
    return default_registry().get_recipe(name)


def registry_snapshot() -> dict[str, Any]:
    return default_registry().to_public()
