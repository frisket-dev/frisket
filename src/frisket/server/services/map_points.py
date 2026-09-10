"""Map-points route service for local server routes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from frisket.authoring.plugin_registry import RuntimeBindingSpec, default_registry
from frisket.engine.projections.point_backend import (
    GeoProjectionBackend,
    GeoProjectionError,
)
from frisket.engine.projections.point_wire import (
    MAP_POINTS_ARROW_MEDIA_TYPE,
    MAP_POINTS_ARROW_SCHEMA,
    serialize_map_points_arrow,
)
from frisket.engine.projections.runtime import (
    ProjectionRuntimeError,
    project_projection_runtime_binding,
    runtime_projection_build,
    runtime_projection_status,
)
from frisket.querysets import SheetRowSetError, resolve_sheet_filter_rows
from frisket.server.workspace import Workspace
from frisket.engine.store import Project
from frisket.server.route_errors import RouteError


@dataclass(frozen=True)
class MapPointsTransport:
    content: bytes
    media_type: str
    headers: dict[str, str]


class MapPointsRouteError(RouteError):
    pass


# The map view's descriptor world is plugin-owned: the map view arrives via
# the bundled geo plugin's descriptor package, and this service refuses with
# a typed 409 (`map_points_binding_missing`) when the project has no active
# role="map_points" runtime projection binding — "disabled geo plugin means
# no map" is the honest semantic.
MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED = True
_MAP_FILTER_BATCH_SIZE = 100_000


class MapPointsService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def sheet_map_points(
        self,
        project_id: str,
        sheet_id: int,
        *,
        column_id: int,
        bbox: str | None = None,
        filter_: str | None = None,
        sort: str | None = None,
        attrs: str | None = None,
        format_: str = "arrow",
    ) -> MapPointsTransport:
        project = self._workspace.get(project_id)
        parsed_bbox = _parse_bbox(bbox)
        if format_ != "arrow":
            raise MapPointsRouteError(
                400,
                {
                    "code": "unsupported_format",
                    "message": "map points are served as Arrow IPC; use format=arrow",
                },
            )

        row_ids: list[int] | None = None
        filter_truncated: bool | None = None
        filter_total: int | None = None
        filter_cap = _map_filter_row_cap()
        if filter_ is not None:
            try:
                row_ids, filter_total = _resolve_filtered_map_rows(
                    project,
                    sheet_id,
                    filter_=filter_,
                    sort=sort,
                    cap=filter_cap,
                )
            except SheetRowSetError as exc:
                raise MapPointsRouteError(
                    400, {"code": "invalid_filter", "message": str(exc)}
                ) from exc
            filter_truncated = filter_total > len(row_ids)

        backend = None
        try:
            try:
                backend = _resolve_map_points_backend(project)
                engine_status = backend.materialize_geo_column(sheet_id, column_id)
                runtime_projection_headers = _map_points_runtime_projection_headers(
                    project,
                    project_id=project_id,
                    sheet_id=sheet_id,
                    column_id=column_id,
                    parsed_bbox=parsed_bbox,
                    filter_=filter_,
                    sort=sort,
                    attrs=attrs,
                    format_=format_,
                    engine_status=engine_status,
                )
                points, status = backend.query_points(
                    sheet_id, column_id, row_ids=row_ids, bbox=parsed_bbox
                )
            except GeoProjectionError as exc:
                http_status = {
                    "not_geo_point": 422,
                    "column_not_found": 404,
                    "sheet_not_found": 404,
                    "map_points_binding_missing": 409,
                }.get(exc.code, 400)
                raise MapPointsRouteError(
                    http_status, {"code": exc.code, "message": exc.message}
                ) from exc
        finally:
            if backend is not None:
                backend.close()

        attributes: dict[str, list[object]] = {}
        if attrs:
            point_row_ids = [pt.row_id for pt in points]
            visible_attr_cols = {
                int(c["id"]) for c in project.columns(sheet_id, include_hidden=False)
            }
            seen: set[int] = set()
            for raw in attrs.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    attr_col_id = int(raw)
                except ValueError:
                    continue
                if attr_col_id in seen or attr_col_id not in visible_attr_cols:
                    continue
                seen.add(attr_col_id)
                vals, _refs = project.get_values_with_refs(
                    sheet_id, attr_col_id, row_ids=point_row_ids
                )
                attributes[f"attr:{attr_col_id}"] = [
                    vals.get(rid) for rid in point_row_ids
                ]

        payload = serialize_map_points_arrow(
            points,
            transient=status["transient"],
            attributes=attributes or None,
            generation=str(status["generation_hash"]),
        )
        headers = {
            "X-Frisket-Map-Schema": MAP_POINTS_ARROW_SCHEMA,
            "X-Frisket-Map-Backend": status["backend_id"],
            "X-Frisket-Map-Backend-Version": status["backend_version"],
            "X-Frisket-Map-Format": "arrow",
            "X-Frisket-Map-Generation": status["generation_hash"],
            "X-Frisket-Map-Transient": "1" if status["transient"] else "0",
            "X-Frisket-Map-Valid-Points": str(status["valid_points"]),
            "Access-Control-Expose-Headers": (
                "X-Frisket-Map-Schema, X-Frisket-Map-Backend, "
                "X-Frisket-Map-Backend-Version, X-Frisket-Map-Generation, "
                "X-Frisket-Map-Format, X-Frisket-Map-Transient, "
                "X-Frisket-Map-Valid-Points, "
                "X-Frisket-Map-Filter-Truncated, X-Frisket-Map-Filter-Total, "
                "X-Frisket-Map-Filter-Cap, "
                "X-Frisket-Runtime-Projection-Kind, "
                "X-Frisket-Runtime-Projection-Status, "
                "X-Frisket-Runtime-Projection-Freshness, "
                "X-Frisket-Runtime-Projection-Generation, "
                "X-Frisket-Runtime-Projection-Artifacts, "
                "X-Frisket-Runtime-Projection-Build-Status, "
                "X-Frisket-Runtime-Projection-Build-Operation, "
                "X-Frisket-Runtime-Projection-Build-Idempotency-Key"
            ),
        }
        headers.update(runtime_projection_headers)
        if filter_truncated is not None:
            headers["X-Frisket-Map-Filter-Truncated"] = "1" if filter_truncated else "0"
            headers["X-Frisket-Map-Filter-Total"] = str(filter_total)
            if filter_cap is not None:
                headers["X-Frisket-Map-Filter-Cap"] = str(filter_cap)
        return MapPointsTransport(
            content=payload,
            media_type=MAP_POINTS_ARROW_MEDIA_TYPE,
            headers=headers,
        )


def _parse_bbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if raw is None:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise MapPointsRouteError(
            400,
            {
                "code": "invalid_bbox",
                "message": "bbox must be minLon,minLat,maxLon,maxLat",
            },
        )
    try:
        min_lon, min_lat, max_lon, max_lat = (float(x) for x in parts)
    except ValueError as exc:
        raise MapPointsRouteError(
            400,
            {"code": "invalid_bbox", "message": "bbox values must be numbers"},
        ) from exc
    if min_lon > max_lon or min_lat > max_lat:
        raise MapPointsRouteError(
            400,
            {
                "code": "invalid_bbox",
                "message": "bbox min values must not exceed max values",
            },
        )
    return min_lon, min_lat, max_lon, max_lat


def _map_filter_row_cap() -> int | None:
    raw = os.environ.get("FRISKET_MAP_FILTER_ROW_CAP")
    if raw is None:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return max(1, cap)


def _resolve_filtered_map_rows(
    project: Project,
    sheet_id: int,
    *,
    filter_: str,
    sort: str | None,
    cap: int | None,
) -> tuple[list[int], int]:
    """Resolve the complete filter in bounded query pages.

    ``FRISKET_MAP_FILTER_ROW_CAP`` remains an explicit deployment truncation
    policy. Without it, the 100k query size is only an internal batch boundary.
    """
    row_ids: list[int] = []
    total = 0
    while True:
        remaining = None if cap is None else cap - len(row_ids)
        if remaining is not None and remaining <= 0:
            break
        batch_size = (
            _MAP_FILTER_BATCH_SIZE
            if remaining is None
            else min(_MAP_FILTER_BATCH_SIZE, remaining)
        )
        rowset = resolve_sheet_filter_rows(
            project,
            sheet_id,
            filter_=filter_,
            sort=sort,
            limit=batch_size,
            offset=len(row_ids),
        )
        total = rowset.total
        row_ids.extend(rowset.row_ids)
        if not rowset.row_ids or len(row_ids) >= total:
            break
    return row_ids, total


def _runtime_projection_error_detail(exc: ProjectionRuntimeError) -> dict[str, Any]:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.projection_kind is not None:
        detail["projection_kind"] = exc.projection_kind
    if exc.field is not None:
        detail["field"] = exc.field
    return detail


def _map_points_runtime_projection_headers(
    project: Project,
    *,
    project_id: str,
    sheet_id: int,
    column_id: int,
    parsed_bbox: tuple[float, float, float, float] | None,
    filter_: str | None,
    sort: str | None,
    attrs: str | None,
    format_: str,
    engine_status: dict[str, Any] | None = None,
) -> dict[str, str]:
    binding = _map_points_runtime_projection_binding(project)
    if binding is None:
        return {}
    projection_kind = binding.kind

    target = {"sheetId": sheet_id, "columnId": column_id}
    params: dict[str, Any] = {"format": format_}
    if parsed_bbox is not None:
        params["bbox"] = list(parsed_bbox)
    if filter_ is not None:
        params["filter"] = filter_
    if sort is not None:
        params["sort"] = sort
    if attrs is not None:
        params["attrs"] = attrs
    if engine_status is not None:
        # Real engine state for the projection contribution to plan against:
        # the host owns the point engine (R*Tree sidecar, generation-hash
        # freshness) and hands its CURRENT state to the plugin's status/build
        # handlers, so the plugin's planning derives from real freshness
        # instead of canned strings.
        params["pointBackend"] = {
            "generationHash": str(engine_status.get("generation_hash") or ""),
            "transient": bool(engine_status.get("transient")),
            "validPoints": engine_status.get("valid_points"),
            "status": str(engine_status.get("status") or ""),
        }

    try:
        status = runtime_projection_status(
            project,
            projection_kind=projection_kind,
            project_id=project_id,
            target=target,
            params=params,
        )
    except ProjectionRuntimeError as exc:
        raise MapPointsRouteError(502, _runtime_projection_error_detail(exc)) from exc

    headers = {
        "X-Frisket-Runtime-Projection-Kind": projection_kind,
        "X-Frisket-Runtime-Projection-Status": status.status,
        "X-Frisket-Runtime-Projection-Freshness": status.freshness.state,
    }
    if status.freshness.generation is not None:
        headers["X-Frisket-Runtime-Projection-Generation"] = status.freshness.generation
    artifact_ids = [ref.artifact_id for ref in status.outputs.artifact_refs]
    if artifact_ids:
        headers["X-Frisket-Runtime-Projection-Artifacts"] = " ".join(artifact_ids)

    should_refresh = status.status in {"stale", "missing", "failed"} or (
        status.freshness.state in {"stale", "missing", "failed"}
    )
    if should_refresh:
        try:
            build = runtime_projection_build(
                project,
                projection_kind=projection_kind,
                project_id=project_id,
                target=target,
                params=params,
                mode="refresh",
            )
        except ProjectionRuntimeError as exc:
            raise MapPointsRouteError(
                502, _runtime_projection_error_detail(exc)
            ) from exc
        if build.status == "failed":
            raise MapPointsRouteError(
                502,
                {
                    "code": "runtime_projection_build_failed",
                    "message": "trusted projection build failed",
                    "projection_kind": projection_kind,
                },
            )
        headers["X-Frisket-Runtime-Projection-Build-Status"] = build.status
        headers["X-Frisket-Runtime-Projection-Build-Operation"] = build.build.operation
        headers["X-Frisket-Runtime-Projection-Build-Idempotency-Key"] = (
            build.build.idempotency_key
        )

    return headers


def _map_points_runtime_projection_binding(
    project: Project,
) -> RuntimeBindingSpec | None:
    for spec in default_registry().runtime_binding_specs("projections"):
        metadata = dict(spec.metadata or {})
        execution = metadata.get("execution")
        if not isinstance(execution, dict) or execution.get("role") != "map_points":
            continue
        binding = project_projection_runtime_binding(project, spec.kind)
        if binding is not None:
            return binding
    return None


def _resolve_map_points_backend(project: Project) -> GeoProjectionBackend:
    """Select the point-serving backend for this request.

    Arrow point bytes always come from the generic host-owned backend
    (design decision: bytes stay host-side -- the deck.gl hot path must never
    receive JSON, and the plugin subprocess transport is line-JSON capped at
    ``projections/runtime.py``'s 100KB, so a plugin can drive the map's
    status/build side-channel but never serve raw points itself). What is
    gated is whether the service serves at all: once the map's descriptor
    world is plugin-owned, refuse with a typed error when this project has no
    active role=`map_points` runtime binding -- "disabled geo plugin means no
    map" is the honest semantic. See
    ``MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED`` above for why that is not
    yet enforced on the live route.
    """
    if MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED:
        if _map_points_runtime_projection_binding(project) is None:
            raise GeoProjectionError(
                "map_points_binding_missing",
                "no plugin owns the map_points projection role for this project",
            )
    return GeoProjectionBackend(project)
