"""Arrow IPC wire format for map points (``frisket.map_points.arrow.v1``).

The deck.gl hot path must never receive ``[{lon, lat, rowId}, ...]`` JSON
objects. The endpoint emits Arrow IPC with typed columns so the browser can
decode lon/lat into deck.gl binary attributes and keep stable ``row_id`` values
for picking.

Logical schema:

- ``row_id``: int64, stable canonical row identifier
- ``lon``: float32
- ``lat``: float32
"""

from __future__ import annotations

from dataclasses import dataclass

MAP_POINTS_ARROW_SCHEMA = "frisket.map_points.arrow.v1"
MAP_POINTS_ARROW_MEDIA_TYPE = "application/vnd.apache.arrow.stream"


@dataclass(frozen=True)
class MapPoint:
    """One materialized map point. ``lon``/``lat`` are validated geographic
    coordinates; ``row_id`` is the stable canonical row identifier the frontend
    uses to open the row drawer."""

    row_id: int
    lon: float
    lat: float


def _attribute_array(values: list[object]):
    """Encode one attribute column. Numeric (incl. all-null) → float64 so the
    client can size/scale; anything else → string (for categorical coloring).
    Booleans are treated as categorical strings, not numbers."""
    import pyarrow as pa

    numeric = all(
        v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))
        for v in values
    )
    if numeric:
        return pa.array(
            [None if v is None else float(v) for v in values], type=pa.float64()
        )
    return pa.array([None if v is None else str(v) for v in values], type=pa.string())


def serialize_map_points_arrow(
    points: list[MapPoint],
    *,
    transient: bool = False,
    attributes: dict[str, list[object]] | None = None,
    generation: str | None = None,
) -> bytes:
    """Pack points into an Arrow IPC stream.

    ``attributes`` maps an Arrow column name (e.g. ``attr:42``) to a per-point
    value list aligned to ``points`` — extra columns the client uses to color or
    size markers (feature: color/size by column). Resolved at query time, so they
    are not part of the projection contract.

    ``generation`` (optional) embeds the projection generation hash as schema
    metadata (``frisket_generation``) so consumers of the RAW bytes — plugin
    views reading through ``ctx.projection.fetchData``, which never see HTTP
    headers — can key fit-once/refit and style-invalidation logic off the same
    generation the ``X-Frisket-Map-Generation`` header carries. Omitted, the
    metadata key is absent and the byte stream is unchanged (the wire-format
    golden pins in tests/test_projection_point_backend_generic.py serialize
    without it).
    """
    import pyarrow as pa

    columns: dict[str, object] = {
        "row_id": pa.array((int(p.row_id) for p in points), type=pa.int64()),
        "lon": pa.array((float(p.lon) for p in points), type=pa.float32()),
        "lat": pa.array((float(p.lat) for p in points), type=pa.float32()),
    }
    for name, values in (attributes or {}).items():
        columns[name] = _attribute_array(values)

    metadata: dict[bytes, bytes] = {
        b"frisket_schema": MAP_POINTS_ARROW_SCHEMA.encode("utf-8"),
        b"frisket_map_points_version": b"1",
        b"frisket_transient": b"1" if transient else b"0",
    }
    if generation is not None:
        metadata[b"frisket_generation"] = generation.encode("utf-8")
    table = pa.table(columns).replace_schema_metadata(metadata)
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()
