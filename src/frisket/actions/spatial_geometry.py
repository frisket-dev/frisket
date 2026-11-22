"""Pure-stdlib point-in-polygon geometry primitives for GeoJSON shapes.

Handles canonical Frisket ``geo_point`` values (``{"lat": ..., "lon": ...}``)
and ``geo_shape`` values (GeoJSON ``Polygon``, ``MultiPolygon``,
``GeometryCollection`` containing polygons, and ``Feature`` wrappers) with
precomputed bounding boxes, exact vertex/edge boundary inclusion, and interior
hole subtraction.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

EPSILON: float = 1e-12
_AREA_EPSILON: float = 1e-15
_MAX_GEOMETRY_DEPTH: int = 8

Point2D = tuple[float, float]
BBox = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class PreparedRing:
    """Validated 2D linear ring with precomputed axis-aligned bounding box."""

    vertices: tuple[Point2D, ...]
    bbox: BBox


@dataclass(frozen=True, slots=True)
class PreparedPolygon:
    """Validated polygon with an exterior ring, optional holes, and bbox."""

    exterior: PreparedRing
    holes: tuple[PreparedRing, ...]
    bbox: BBox


@dataclass(frozen=True, slots=True)
class PreparedShape:
    """One or more polygons compiled with precomputed bounding boxes."""

    polygons: tuple[PreparedPolygon, ...]
    bbox: BBox


def _to_finite_float(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def parse_geo_point(value: Any) -> Point2D | None:
    """Parse a canonical ``geo_point`` cell into ``(lon, lat)`` or ``None``.

    Matches ``frisket.authoring.column_types._is_geo_point``: accepts either a
    dict or a JSON string encoding ``{"lat": ..., "lon": ...}`` with finite
    numeric coordinates in ``[-90, 90]`` and ``[-180, 180]``.
    """
    parsed = value
    if isinstance(parsed, str):
        stripped = parsed.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return None
    if not isinstance(parsed, dict):
        return None
    lat_f = _to_finite_float(parsed.get("lat"))
    lon_f = _to_finite_float(parsed.get("lon"))
    if lat_f is None or lon_f is None:
        return None
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lon_f <= 180.0):
        return None
    return (lon_f, lat_f)


def _parse_position(pos: Any) -> Point2D | None:
    if not isinstance(pos, (list, tuple)) or len(pos) < 2:
        return None
    lon_f = _to_finite_float(pos[0])
    lat_f = _to_finite_float(pos[1])
    if lon_f is None or lat_f is None:
        return None
    return (lon_f, lat_f)


def _ring_bbox(vertices: list[Point2D]) -> BBox:
    lons = [pt[0] for pt in vertices]
    lats = [pt[1] for pt in vertices]
    return (min(lons), min(lats), max(lons), max(lats))


def _has_nonzero_area(vertices: list[Point2D], bbox: BBox) -> bool:
    span_x = bbox[2] - bbox[0]
    span_y = bbox[3] - bbox[1]
    scale = span_x if span_x >= span_y else span_y
    if not math.isfinite(scale) or scale <= 0.0:
        return False
    x0, y0 = vertices[0]
    total = 0.0
    j = len(vertices) - 1
    for i, (xi, yi) in enumerate(vertices):
        xj, yj = vertices[j]
        total += ((xj - x0) / scale) * ((yi - y0) / scale) - ((xi - x0) / scale) * (
            (yj - y0) / scale
        )
        j = i
    return math.isfinite(total) and abs(total) > _AREA_EPSILON


def _parse_ring(raw_ring: Any) -> PreparedRing | None:
    if not isinstance(raw_ring, (list, tuple)) or len(raw_ring) < 3:
        return None
    vertices: list[Point2D] = []
    for pos in raw_ring:
        pt = _parse_position(pos)
        if pt is None:
            return None
        if not vertices or vertices[-1] != pt:
            vertices.append(pt)
    if len(vertices) > 1 and vertices[-1] == vertices[0]:
        vertices.pop()
    if len(set(vertices)) < 3:
        return None
    bbox = _ring_bbox(vertices)
    if not _has_nonzero_area(vertices, bbox):
        return None
    return PreparedRing(vertices=tuple(vertices), bbox=bbox)


def _parse_polygon_coords(raw_coords: Any) -> PreparedPolygon | None:
    if not isinstance(raw_coords, (list, tuple)) or not raw_coords:
        return None
    exterior = _parse_ring(raw_coords[0])
    if exterior is None:
        return None
    holes: list[PreparedRing] = []
    for raw_hole in raw_coords[1:]:
        hole = _parse_ring(raw_hole)
        if hole is None:
            return None
        holes.append(hole)
    return PreparedPolygon(
        exterior=exterior,
        holes=tuple(holes),
        bbox=exterior.bbox,
    )


def _extract_multipolygon(raw_polys: Any) -> list[PreparedPolygon] | None:
    if not isinstance(raw_polys, (list, tuple)) or not raw_polys:
        return None
    polys: list[PreparedPolygon] = []
    for raw_poly in raw_polys:
        poly = _parse_polygon_coords(raw_poly)
        if poly is None:
            return None
        polys.append(poly)
    return polys


def _extract_collection(geometries: Any, *, depth: int) -> list[PreparedPolygon] | None:
    if not isinstance(geometries, (list, tuple)):
        return None
    collected: list[PreparedPolygon] = []
    for member in geometries:
        extracted = _extract_polygons(member, depth=depth + 1)
        if extracted:
            collected.extend(extracted)
    return collected or None


def _extract_polygons(obj: Any, *, depth: int = 0) -> list[PreparedPolygon] | None:
    if depth > _MAX_GEOMETRY_DEPTH or not isinstance(obj, dict):
        return None
    geo_type = obj.get("type")
    if geo_type == "Feature":
        return _extract_polygons(obj.get("geometry"), depth=depth + 1)
    if geo_type == "Polygon":
        poly = _parse_polygon_coords(obj.get("coordinates"))
        return [poly] if poly is not None else None
    if geo_type == "MultiPolygon":
        return _extract_multipolygon(obj.get("coordinates"))
    if geo_type == "GeometryCollection":
        return _extract_collection(obj.get("geometries"), depth=depth)
    return None


def prepare_shape(shape: Any) -> PreparedShape | None:
    """Compile a GeoJSON polygon shape once with precomputed bounding boxes.

    Accepts dicts, JSON strings, GeoJSON ``Polygon``, ``MultiPolygon``,
    ``GeometryCollection`` (extracting polygon members), and ``Feature``
    wrappers. Returns ``None`` for null, malformed, or non-polygon geometries.
    """
    if isinstance(shape, PreparedShape):
        return shape
    parsed = shape
    if isinstance(parsed, str):
        stripped = parsed.strip()
        if not stripped:
            return None
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            return None
    polys = _extract_polygons(parsed)
    if not polys:
        return None
    min_lon = min(poly.bbox[0] for poly in polys)
    min_lat = min(poly.bbox[1] for poly in polys)
    max_lon = max(poly.bbox[2] for poly in polys)
    max_lat = max(poly.bbox[3] for poly in polys)
    return PreparedShape(
        polygons=tuple(polys),
        bbox=(min_lon, min_lat, max_lon, max_lat),
    )


def _point_in_bbox(lon: float, lat: float, bbox: BBox, eps: float) -> bool:
    return (
        bbox[0] - eps <= lon <= bbox[2] + eps and bbox[1] - eps <= lat <= bbox[3] + eps
    )


def _point_on_segment(
    point: Point2D,
    start: Point2D,
    end: Point2D,
    eps: float,
) -> bool:
    px, py = point
    x1, y1 = start
    x2, y2 = end
    min_x = x1 if x1 < x2 else x2
    max_x = x2 if x1 < x2 else x1
    if px < min_x - eps or px > max_x + eps:
        return False
    min_y = y1 if y1 < y2 else y2
    max_y = y2 if y1 < y2 else y1
    if py < min_y - eps or py > max_y + eps:
        return False

    dx = x2 - x1
    dy = y2 - y1
    if not (math.isfinite(dx) and math.isfinite(dy)):
        return False
    seg_len = math.hypot(dx, dy)
    if seg_len == 0.0:
        return math.hypot(px - x1, py - y1) <= eps

    ux = dx / seg_len
    uy = dy / seg_len
    proj = (px - x1) * ux + (py - y1) * uy
    proj = max(0.0, min(seg_len, proj))
    nearest_x = x1 + proj * ux
    nearest_y = y1 + proj * uy
    return math.hypot(px - nearest_x, py - nearest_y) <= eps


def _point_on_ring(point: Point2D, ring: PreparedRing, eps: float) -> bool:
    px, py = point
    if not _point_in_bbox(px, py, ring.bbox, eps):
        return False
    verts = ring.vertices
    j = len(verts) - 1
    for i in range(len(verts)):
        if _point_on_segment(point, verts[j], verts[i], eps):
            return True
        j = i
    return False


def _point_strictly_inside_ring(px: float, py: float, ring: PreparedRing) -> bool:
    inside = False
    verts = ring.vertices
    j = len(verts) - 1
    for i in range(len(verts)):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if (yi > py) != (yj > py):
            ratio = (py - yi) / (yj - yi)
            x_intersect = xi + ratio * (xj - xi)
            if px < x_intersect:
                inside = not inside
        j = i
    return inside


def _point_in_polygon(px: float, py: float, poly: PreparedPolygon, eps: float) -> bool:
    if not _point_in_bbox(px, py, poly.bbox, eps):
        return False
    point = (px, py)
    if not _point_on_ring(point, poly.exterior, eps):
        if not _point_strictly_inside_ring(px, py, poly.exterior):
            return False
    for hole in poly.holes:
        if not _point_in_bbox(px, py, hole.bbox, eps):
            continue
        if _point_on_ring(point, hole, eps):
            # Boundary points of a hole belong to the polygon boundary.
            continue
        if _point_strictly_inside_ring(px, py, hole):
            return False
    return True


def point_in_prepared_shape(
    lon: float,
    lat: float,
    prepared: PreparedShape,
    *,
    eps: float = EPSILON,
) -> bool:
    """Return True if ``(lon, lat)`` falls inside or on ``prepared``."""
    px = _to_finite_float(lon)
    py = _to_finite_float(lat)
    if px is None or py is None:
        return False
    if not _point_in_bbox(px, py, prepared.bbox, eps):
        return False
    for poly in prepared.polygons:
        if _point_in_polygon(px, py, poly, eps):
            return True
    return False


def point_in_shape(
    lon: float,
    lat: float,
    shape: Any,
    *,
    eps: float = EPSILON,
) -> bool:
    """Return True if ``(lon, lat)`` lies inside or on a GeoJSON polygon shape.

    Returns ``False`` cleanly for null, NaN/Inf, malformed, or non-polygon
    geometries (such as ``Point`` or ``LineString``).
    """
    px = _to_finite_float(lon)
    py = _to_finite_float(lat)
    if px is None or py is None:
        return False
    prepared = prepare_shape(shape)
    if prepared is None:
        return False
    return point_in_prepared_shape(px, py, prepared, eps=eps)
