"""Project-scoped model route validation, materialization, and pinning."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from frisket.ai.llm.model_catalog import MODEL_ENTRIES
from frisket.cloud_targets import cloud_fallback_is_blocked
from frisket.execution.resolver import (
    ModelRoute,
    Refusal,
    canonical_model_route_id,
    cloud_route_eligible,
    model_route_for_target,
    preferred_route_candidate,
)
from frisket.execution.targets import EXECUTION_CAPABILITIES, CAPABILITY_LLM
from frisket.local_model_ids import format_local_model_id
from frisket.project_settings import (
    ProjectSettings,
    read_project_settings,
    write_project_settings,
)
from frisket.server.provider_config import build_provider_catalog
from frisket.server.workspace import Workspace


class ProjectModelRouteError(ValueError):
    """A model route mutation is invalid for this project inventory."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class ProjectModelRouteSelection:
    model_id: str
    route: ModelRoute


@dataclass
class _ModelInventoryEntry:
    variants: list[tuple[str, str]] = field(default_factory=list)
    has_own: bool = False
    has_cloud: bool = False
    local_only: bool = False
    has_nonlocal: bool = False


class ProjectModelRoutesService:
    """Own the one settings mutation seam for model execution routes."""

    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def set_route(
        self,
        project_id: str,
        model_id: str,
        route: ModelRoute,
    ) -> ProjectModelRouteSelection:
        if route not in {"cloud", "own"}:
            raise ProjectModelRouteError(
                422,
                "invalid_model_route",
                "Model route must be 'cloud' or 'own'.",
            )
        project = self._workspace.get(project_id)
        composition, inventory = self._context(project)
        canonical = self._known_model_id(model_id, inventory)
        entry = inventory.get(canonical) if canonical is not None else None
        if canonical is None or entry is None:
            raise ProjectModelRouteError(
                422,
                "unknown_model",
                "Choose a model from this project's current model catalog.",
            )
        self._validate_route(canonical, route, entry, composition)
        with project.transaction_scope():
            settings = self._materialized_settings(project, composition, inventory)
            routes = dict(settings.model_execution_routes or {})
            routes[canonical] = route
            write_project_settings(
                project,
                settings.model_copy(update={"model_execution_routes": routes}),
                commit=False,
            )
        return ProjectModelRouteSelection(model_id=canonical, route=route)

    def pin_ready_routes(
        self,
        project_id: str,
        *,
        commit: bool = True,
    ) -> ProjectSettings:
        project = self._workspace.get(project_id)
        if commit:
            with project.transaction_scope():
                return self._pin_ready_routes(project)
        if not project.db.in_transaction:
            raise RuntimeError("commit=False requires a caller-owned transaction")
        return self._pin_ready_routes(project)

    def pin_cloud_connection_routes(
        self,
        user_id: int,
        *,
        initiating_project_id: str | None = None,
    ) -> None:
        """Pin matching-user projects before publishing a device connection."""

        project_ids = [
            str(row["id"])
            for row in self._workspace.list()
            if isinstance(row, dict) and isinstance(row.get("id"), str)
        ]
        selected = {
            project_id
            for project_id in project_ids
            if read_project_settings(self._workspace.get(project_id)).cloud_user_id
            == user_id
        }
        if initiating_project_id is not None:
            selected.add(initiating_project_id)
        for project_id in project_ids:
            if project_id in selected:
                self.pin_ready_routes(project_id)
        if (
            initiating_project_id is not None
            and initiating_project_id not in project_ids
        ):
            self.pin_ready_routes(initiating_project_id)

    def pin_all_projects(self) -> None:
        """Pin current static choices before an installation config mutation."""

        for row in self._workspace.list():
            project_id = row.get("id") if isinstance(row, dict) else None
            if isinstance(project_id, str):
                self.pin_ready_routes(project_id)

    def _pin_ready_routes(self, project: Any) -> ProjectSettings:
        composition, inventory = self._context(project)
        materialized = self._materialized_settings(project, composition, inventory)
        return write_project_settings(project, materialized, commit=False)

    def _context(self, project: Any) -> tuple[Any, dict[str, _ModelInventoryEntry]]:
        router = self._workspace.router_for(project)
        composition = self._workspace.execution_composition_for(
            project,
            router,
            self._workspace.edition_execution_composition_context_for(),
        )
        return composition, self._inventory(router, composition)

    def _inventory(
        self, router: Any, composition: Any
    ) -> dict[str, _ModelInventoryEntry]:
        inventory: dict[str, _ModelInventoryEntry] = {}
        catalog = build_provider_catalog(
            self._workspace.root,
            network_off=False,
            # Pinning records the route that static configuration would choose;
            # it must not turn every project into an Ollama reachability probe.
            # Exact non-catalog supports are added from the composition below.
            local_endpoints=(),
            local_endpoint_authority=(
                "instance" if self._workspace.edition == "solo" else "organization"
            ),
        )
        for provider in catalog.get("providers", []):
            if not isinstance(provider, dict):
                continue
            local_http = provider.get("kind") == "local_http"
            for model in provider.get("models", []):
                raw_engine = model.get("id") if isinstance(model, dict) else None
                if not isinstance(raw_engine, str):
                    continue
                canonical = canonical_model_route_id(raw_engine, CAPABILITY_LLM)
                if canonical is None:
                    continue
                entry = inventory.setdefault(canonical, _ModelInventoryEntry())
                variant = (CAPABILITY_LLM, raw_engine)
                if variant not in entry.variants:
                    entry.variants.append(variant)
                if local_http:
                    entry.has_own = True
                    entry.local_only = not entry.has_nonlocal
                else:
                    entry.has_own = True
                    entry.has_nonlocal = True
                    entry.local_only = False

        # Configured endpoint identities and the built-in local model catalog
        # are static facts. Preserve those routes without probing which models
        # happen to be reachable or installed right now.
        for endpoint in router.local_endpoints:
            for model in MODEL_ENTRIES.get("ollama", []):
                raw_engine = format_local_model_id(endpoint.endpoint_id, model["id"])
                canonical = canonical_model_route_id(raw_engine, CAPABILITY_LLM)
                if canonical is None:
                    continue
                entry = inventory.setdefault(canonical, _ModelInventoryEntry())
                variant = (CAPABILITY_LLM, raw_engine)
                if variant not in entry.variants:
                    entry.variants.append(variant)
                entry.has_own = True
                entry.local_only = not entry.has_nonlocal

        provider = composition.provider_for_resolution()
        for target in provider.targets():
            for support in target.engines:
                if support.engine.endswith("/*"):
                    continue
                canonical = canonical_model_route_id(
                    support.engine,
                    support.capability,
                )
                if canonical is None:
                    continue
                entry = inventory.setdefault(canonical, _ModelInventoryEntry())
                variant = (support.capability, support.engine)
                if variant not in entry.variants:
                    entry.variants.append(variant)
                route = model_route_for_target(
                    provider,
                    target,
                    support.capability,
                    canonical,
                )
                if route == "cloud":
                    entry.has_cloud = True
                    entry.has_nonlocal = True
                    entry.local_only = False
                else:
                    entry.has_own = True
                    if target.egress_class == "none" and not entry.has_nonlocal:
                        entry.local_only = True
                    elif target.egress_class != "none":
                        entry.has_nonlocal = True
                        entry.local_only = False
                if cloud_fallback_is_blocked(
                    composition,
                    target_id=target.id,
                    capability=support.capability,
                    engine=canonical,
                ):
                    entry.has_cloud = True
                    entry.local_only = False
        return inventory

    @staticmethod
    def _known_model_id(
        raw_model_id: str,
        inventory: dict[str, _ModelInventoryEntry],
    ) -> str | None:
        if raw_model_id in inventory:
            return raw_model_id
        matches = {
            canonical
            for capability in EXECUTION_CAPABILITIES
            if (canonical := canonical_model_route_id(raw_model_id, capability))
            in inventory
        }
        return next(iter(matches)) if len(matches) == 1 else None

    @staticmethod
    def _validate_route(
        model_id: str,
        route: ModelRoute,
        entry: _ModelInventoryEntry,
        composition: Any,
    ) -> None:
        if route == "cloud":
            cloud_eligible = entry.has_cloud or any(
                cloud_route_eligible(raw_engine, capability)
                for capability, raw_engine in entry.variants
            )
            if entry.local_only:
                raise ProjectModelRouteError(
                    409,
                    "cloud_route_unsupported",
                    f"Model {model_id!r} runs only on this installation.",
                )
            if not cloud_eligible:
                raise ProjectModelRouteError(
                    409,
                    "cloud_route_unsupported",
                    f"Model {model_id!r} is not offered through Frisket Cloud.",
                )
            if composition.cloud_inventory_known and not entry.has_cloud:
                raise ProjectModelRouteError(
                    409,
                    "cloud_route_unsupported",
                    f"Frisket Cloud does not offer model {model_id!r}.",
                )
        elif not entry.has_own:
            raise ProjectModelRouteError(
                409,
                "own_route_unsupported",
                f"Model {model_id!r} has no own-key or models-server route.",
            )

    def _materialized_settings(
        self,
        project: Any,
        composition: Any,
        inventory: dict[str, _ModelInventoryEntry],
    ) -> ProjectSettings:
        settings = read_project_settings(project)
        provider = composition.provider_for_resolution()
        routes: dict[str, ModelRoute] = dict(settings.model_execution_routes or {})
        for model_id, entry in inventory.items():
            if model_id in routes:
                continue
            for capability, raw_engine in entry.variants:
                choice = preferred_route_candidate(
                    raw_engine,
                    provider,
                    capability=capability,
                )
                if choice is None:
                    continue
                if isinstance(choice, Refusal):
                    raise ProjectModelRouteError(
                        409,
                        "model_route_configuration_unavailable",
                        choice.remedy,
                    )
                target, support = choice
                if (
                    provider.configuration(
                        target.id,
                        support.capability,
                        support.engine,
                    )
                    != "configured"
                ):
                    continue
                route = model_route_for_target(
                    provider,
                    target,
                    support.capability,
                    support.engine,
                )
                if cloud_fallback_is_blocked(
                    composition,
                    target_id=target.id,
                    capability=support.capability,
                    engine=support.engine,
                ):
                    route = "cloud"
                routes[model_id] = route
                break
        model_route_default = settings.model_execution_default
        if settings.model_execution_routes is None:
            legacy_preference = self._workspace.legacy_cloud_execution_preference(
                project
            )
            model_route_default = (
                "cloud" if legacy_preference == "cloud_first" else None
            )
        return settings.model_copy(
            update={
                "model_execution_routes": routes,
                "model_execution_default": model_route_default,
            }
        )
