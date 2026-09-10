"""Action registry route service."""

from __future__ import annotations

from typing import Any

from frisket.authoring.recipe_registry import (
    ActionRegistryError,
    action_registry_entry_summary,
    build_action_artifact,
    validate_action_artifact,
)
from frisket.server.services import saved_actions
from frisket.server.workspace import Workspace
from frisket.server.route_errors import RouteError


class ActionRegistryRouteError(RouteError):
    pass


class ActionRegistryService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def publish_artifact(
        self,
        *,
        saved_action_id: str | None,
        spec: dict[str, Any] | None,
        name: str | None,
        description: str | None,
        publisher: dict[str, Any] | None,
        dataset: dict[str, Any] | None,
        checks: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        if saved_action_id is not None and spec is not None:
            raise ActionRegistryRouteError(
                400, "pass saved_action_id or spec, not both"
            )
        if saved_action_id is not None:
            saved = self._workspace.saved_recipe_by_id(saved_action_id)
            if saved is None:
                raise ActionRegistryRouteError(404, "no such saved action")
            try:
                artifact_spec = saved_actions.require_saved_action_spec(
                    saved.get("spec") or {}
                )
            except saved_actions.SavedActionSpecError as exc:
                raise ActionRegistryRouteError(400, str(exc)) from exc
            artifact_name = name or saved.get("name") or "action"
        elif spec is not None:
            try:
                artifact_spec = saved_actions.require_saved_action_spec(spec)
            except saved_actions.SavedActionSpecError as exc:
                raise ActionRegistryRouteError(400, str(exc)) from exc
            artifact_name = name or str(spec.get("action_name") or "action")
        else:
            raise ActionRegistryRouteError(400, "spec or saved_action_id is required")
        try:
            artifact = build_action_artifact(
                name=artifact_name,
                spec=artifact_spec,
                description=description,
                publisher=publisher,
                dataset=dataset,
                checks=checks,
            )
            stored = self._workspace.action_registry.publish(artifact)
        except ActionRegistryError as exc:
            raise ActionRegistryRouteError(400, str(exc)) from exc
        return {
            "schema_version": "frisket.action_registry_publish.v1",
            "artifact_id": stored["artifact_id"],
            "digest": stored["digest"],
            "entry": action_registry_entry_summary(stored),
            "artifact": stored,
            "receipt": stored["receipts"][0],
        }

    def list_registry(self) -> dict[str, Any]:
        return self._workspace.action_registry.list()

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        try:
            artifact = self._workspace.action_registry.get(artifact_id)
        except ActionRegistryError as exc:
            raise ActionRegistryRouteError(400, str(exc)) from exc
        if artifact is None:
            raise ActionRegistryRouteError(404, "no such action artifact")
        return {"artifact": artifact}

    def import_artifact(
        self,
        *,
        artifact_id: str | None,
        artifact: dict[str, Any] | None,
        name: str | None,
    ) -> dict[str, Any]:
        if artifact_id is not None and artifact is not None:
            raise ActionRegistryRouteError(
                400, "pass artifact_id or artifact, not both"
            )
        if artifact_id is not None:
            try:
                candidate = self._workspace.action_registry.get(artifact_id)
            except ActionRegistryError as exc:
                raise ActionRegistryRouteError(400, str(exc)) from exc
            if candidate is None:
                raise ActionRegistryRouteError(404, "no such action artifact")
        elif artifact is not None:
            try:
                candidate = validate_action_artifact(artifact)
            except ActionRegistryError as exc:
                raise ActionRegistryRouteError(400, str(exc)) from exc
        else:
            raise ActionRegistryRouteError(400, "artifact_id or artifact is required")
        action_name = (name if name is not None else candidate["name"]).strip()
        if not action_name:
            raise ActionRegistryRouteError(400, "name cannot be blank")
        try:
            stored = self._workspace.action_registry.publish(candidate)
        except ActionRegistryError as exc:
            raise ActionRegistryRouteError(400, str(exc)) from exc
        entry = self._workspace.save_recipe(
            action_name,
            stored["spec"],
            registry_artifact=action_registry_entry_summary(stored),
            eval_receipts=stored.get("receipts", []),
        )
        return {
            "schema_version": "frisket.action_registry_import.v1",
            "saved_action": saved_actions.saved_action_template(entry),
            "artifact": action_registry_entry_summary(stored),
        }
