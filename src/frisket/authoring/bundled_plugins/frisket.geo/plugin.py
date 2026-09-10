"""Backend handlers for the bundled geo plugin.

One contribution: the role-``map_points`` runtime projection. The host owns
the point engine (R*Tree sidecar + generation-hash freshness,
``frisket/projections/point_backend.py``) and Arrow byte serving — this
handler owns the projection's status/build PLANNING, derived from the REAL
engine state the map-points service passes through
(``params["pointBackend"]``, ``server/services/map_points.py``). It never
invents generations and never serves point bytes (the runtime_plan contract
is bounded JSON, ``frisket/projections/runtime.py``).
"""

from __future__ import annotations

from typing import Any

from frisket.plugins.sdk import Plugin

plugin = Plugin()

_STATUS_SCHEMA = "frisket.runtime_projection_status.v1"
_BUILD_PLAN_SCHEMA = "frisket.runtime_projection_build_plan.v1"


def _engine_state(params: dict[str, Any]) -> dict[str, Any]:
    engine = params.get("pointBackend")
    return engine if isinstance(engine, dict) else {}


def _artifact_refs(
    projection_kind: str, generation: str | None
) -> list[dict[str, Any]]:
    if not generation:
        return []
    return [
        {
            "kind": "projection_artifact",
            "projectionKind": projection_kind,
            "artifactId": f"projection://map-points/{generation}",
        }
    ]


@plugin.projection(
    "map_points",
    handler_key="map_points",
    title="Map points projection",
)
async def map_points_projection(
    ctx,
    _rows: list[dict[str, Any]],
    *,
    target: dict[str, Any],
    params: dict[str, Any],
    mode: str = "status",
) -> dict[str, Any]:
    del target
    projection_kind = ctx.projection_kind
    engine = _engine_state(params)
    generation = str(engine.get("generationHash") or "") or None
    transient = bool(engine.get("transient"))
    valid_points = engine.get("validPoints")
    metrics: dict[str, Any] = {"handler": "frisket.geo:map_points"}
    if isinstance(valid_points, (int, float)):
        metrics["validPoints"] = int(valid_points)

    if mode == "status":
        if generation is None:
            # No engine state in the request (e.g. a frontend status probe
            # outside the map-points service): the honest claim is that
            # freshness cannot be confirmed from here.
            return {
                "schemaVersion": _STATUS_SCHEMA,
                "status": "stale",
                "freshness": {"state": "stale", "generation": None, "transient": False},
                "outputs": {"artifactRefs": [], "metrics": metrics},
                "warnings": [
                    "no pointBackend engine state in params; freshness "
                    "unconfirmed outside the map-points service"
                ],
            }
        if transient:
            # The engine is mid-rebuild for this generation.
            return {
                "schemaVersion": _STATUS_SCHEMA,
                "status": "building",
                "freshness": {
                    "state": "transient",
                    "generation": generation,
                    "transient": True,
                },
                "outputs": {
                    "artifactRefs": _artifact_refs(projection_kind, generation),
                    "metrics": metrics,
                },
                "warnings": [],
            }
        return {
            "schemaVersion": _STATUS_SCHEMA,
            "status": "ready",
            "freshness": {
                "state": "fresh",
                "generation": generation,
                "transient": False,
            },
            "outputs": {
                "artifactRefs": _artifact_refs(projection_kind, generation),
                "metrics": metrics,
            },
            "warnings": [],
        }

    # Build planning: the engine rebuilds lazily by generation hash, so a
    # refresh against a known-fresh generation is a no-op; anything else is
    # an accepted refresh/rebuild keyed to the real generation.
    operation = "rebuild" if mode == "rebuild" else "refresh"
    if operation == "refresh" and generation is not None and not transient:
        return {
            "schemaVersion": _BUILD_PLAN_SCHEMA,
            "status": "noop",
            "build": {
                "operation": "noop",
                "idempotencyKey": f"map-points@{generation}:refresh",
            },
            "outputs": {
                "artifactRefs": _artifact_refs(projection_kind, generation),
                "metrics": metrics,
            },
            "warnings": [],
        }
    return {
        "schemaVersion": _BUILD_PLAN_SCHEMA,
        "status": "accepted",
        "build": {
            "operation": operation,
            "idempotencyKey": f"map-points@{generation or 'unknown'}:{operation}",
        },
        "outputs": {
            "artifactRefs": _artifact_refs(projection_kind, generation),
            "metrics": metrics,
        },
        "warnings": [],
    }
