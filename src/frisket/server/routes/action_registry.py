"""Action registry route registration."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from frisket.server import schemas
from frisket.server.services.action_registry import (
    ActionRegistryService,
)


def register_action_registry_routes(
    app: FastAPI,
    *,
    service: ActionRegistryService,
) -> None:
    """Public action-registry surface (publish/list/get/import).

    All four routes implement the public action-registry contract —
    "The public action registry now lives at
    /api/actions/v1/registry/artifacts and /api/actions/v1/registry/imports"
    (the recipes->actions registry cutover that removed /api/recipes/*) —
    skill bundles use this same registry (same curation/signing/pinning).
    Presence is pinned by the
    registry contract tests (tests/test_action_registry.py and the
    recipe-registry and saved-action removal-tombstone suites; this module's
    binding-only test bans their literal filenames here). Its
    removal condition requires a versioned public
    contract change that supersedes the v1-closure registry-cutover entry and
    retires those tests plus the registry endpoint-catalog entries together.
    """

    @app.post("/api/actions/v1/registry/artifacts")
    def publish_action_artifact(
        body: schemas.PublishActionArtifactBody,
    ) -> dict[str, Any]:
        """Publish a saved action as a content-addressed registry artifact.

        Retained per the public-registry contract documented on
        register_action_registry_routes, with the removal condition
        recorded there.
        """
        return service.publish_artifact(
            saved_action_id=body.saved_action_id,
            spec=body.spec,
            name=body.name,
            description=body.description,
            publisher=body.publisher,
            dataset=body.dataset,
            checks=body.checks,
        )

    @app.get("/api/actions/v1/registry/artifacts")
    def list_action_registry() -> dict[str, Any]:
        """List registry artifacts.

        KEPT per the public-registry contract documented on
        register_action_registry_routes, with the removal condition
        recorded there.
        """
        return service.list_registry()

    @app.get("/api/actions/v1/registry/artifacts/{artifact_id}")
    def get_action_registry_artifact(artifact_id: str) -> dict[str, Any]:
        """Fetch one registry artifact by id.

        KEPT per the public-registry contract documented on
        register_action_registry_routes, with the removal condition
        recorded there.
        """
        return service.get_artifact(artifact_id)

    @app.post("/api/actions/v1/registry/imports")
    def import_action_artifact(
        body: schemas.ImportActionArtifactBody,
    ) -> dict[str, Any]:
        """Import a registry artifact (by id or inline, receipt-verified).

        KEPT per the public-registry contract documented on
        register_action_registry_routes, with the removal condition
        recorded there.
        """
        return service.import_artifact(
            artifact_id=body.artifact_id,
            artifact=body.artifact,
            name=body.name,
        )
