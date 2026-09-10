"""lxml-only KML/KMZ reader mapping placemarks to GeoJSON geometry dicts.

Chosen over ``fastkml`` per the geo-import-expansion spec's dependency decision:
``lxml`` is already a resident core-chain dependency (via ``trafilatura``) and is
promoted to a declared core dep here, so no new package (and no ``pygeoif`` /
``arrow`` transitive weight) is pulled in. The reader is namespace-agnostic
(matches elements by local name, so KML 2.2 / 2.3 / the legacy
``earth.google.com`` namespace all parse) and drops KML altitude (a 3rd
coordinate) to 2D lon/lat, matching what ``geo_shape`` stores.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from typing import Any

try:  # pragma: no cover - exercised via importorskip in tests
    from lxml import etree
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "import.kml requires the 'lxml' core dependency; declare it in pyproject."
    ) from exc


class KmlParseError(Exception):
    """The KML/KMZ source could not be parsed into placemarks."""


_GEOMETRY_TAGS = ("Point", "LineString", "Polygon", "MultiGeometry")


@dataclass
class KmlPlacemark:
    """One KML ``<Placemark>`` mapped toward the ``geo_shape`` cell shape.

    ``geometry`` is a GeoJSON geometry dict (or ``None`` when the placemark has
    no supported geometry element). ``extended_data`` maps ExtendedData /
    SchemaData keys to their string values.
    """

    name: str | None
    description: str | None
    extended_data: dict[str, str | None] = field(default_factory=dict)
    geometry: dict[str, Any] | None = None


def _localname(tag: Any) -> str:
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag if isinstance(tag, str) else ""


def _child(el: Any, name: str) -> Any | None:
    for c in el:
        if _localname(c.tag) == name:
            return c
    return None


def _children(el: Any, name: str) -> list[Any]:
    return [c for c in el if _localname(c.tag) == name]


def _descendants(el: Any, name: str) -> list[Any]:
    return [
        e for e in el.iter() if isinstance(e.tag, str) and _localname(e.tag) == name
    ]


def _parse_positions(text: str | None) -> list[list[float]]:
    """Parse a KML ``<coordinates>`` blob into a list of ``[lon, lat]`` pairs.

    KML coordinate tuples are ``lon,lat[,alt]`` separated by whitespace; the
    altitude (a 3rd value) is dropped to keep 2D lon/lat.
    """
    positions: list[list[float]] = []
    for token in (text or "").split():
        parts = token.split(",")
        if len(parts) < 2:
            raise KmlParseError(f"malformed KML coordinate tuple: {token!r}")
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError as exc:
            raise KmlParseError(f"non-numeric KML coordinate tuple: {token!r}") from exc
        positions.append([lon, lat])
    return positions


def _combine_multi(geoms: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not geoms:
        return None
    types = {g["type"] for g in geoms}
    if types == {"Point"}:
        return {"type": "MultiPoint", "coordinates": [g["coordinates"] for g in geoms]}
    if types == {"LineString"}:
        return {
            "type": "MultiLineString",
            "coordinates": [g["coordinates"] for g in geoms],
        }
    if types == {"Polygon"}:
        return {
            "type": "MultiPolygon",
            "coordinates": [g["coordinates"] for g in geoms],
        }
    return {"type": "GeometryCollection", "geometries": geoms}


def _convert_geometry(el: Any) -> dict[str, Any] | None:
    tag = _localname(el.tag)
    if tag == "Point":
        coords = _descendants(el, "coordinates")
        positions = _parse_positions(coords[0].text) if coords else []
        if not positions:
            return None
        return {"type": "Point", "coordinates": positions[0]}
    if tag == "LineString":
        coords = _descendants(el, "coordinates")
        positions = _parse_positions(coords[0].text) if coords else []
        return {"type": "LineString", "coordinates": positions}
    if tag == "Polygon":
        rings: list[list[list[float]]] = []
        for boundary in _children(el, "outerBoundaryIs") + _children(
            el, "innerBoundaryIs"
        ):
            coords = _descendants(boundary, "coordinates")
            if coords:
                rings.append(_parse_positions(coords[0].text))
        return {"type": "Polygon", "coordinates": rings}
    if tag == "MultiGeometry":
        geoms: list[dict[str, Any]] = []
        for c in el:
            if _localname(c.tag) in _GEOMETRY_TAGS:
                converted = _convert_geometry(c)
                if converted is not None:
                    geoms.append(converted)
        return _combine_multi(geoms)
    return None


def _geometry_element(placemark: Any) -> Any | None:
    for c in placemark:
        if _localname(c.tag) in _GEOMETRY_TAGS:
            return c
    return None


def _extended_data(placemark: Any) -> dict[str, str | None]:
    data: dict[str, str | None] = {}
    for ed in _children(placemark, "ExtendedData"):
        for d in _descendants(ed, "Data"):
            key = d.get("name")
            if key is not None:
                value_el = _child(d, "value")
                data[key] = value_el.text if value_el is not None else None
        for sd in _descendants(ed, "SimpleData"):
            key = sd.get("name")
            if key is not None:
                data[key] = sd.text
    return data


def _unwrap_kmz(data: bytes) -> bytes:
    """Return KML bytes, unzipping a KMZ (zip) archive to its ``.kml`` entry.

    KMZ's canonical root is ``doc.kml``; some producers (e.g. the NPS export)
    name the inner entry differently, so fall back to the first ``.kml`` member.
    """
    if data[:2] != b"PK":
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
            target = "doc.kml" if "doc.kml" in names else None
            if target is None:
                target = next((n for n in names if n.lower().endswith(".kml")), None)
            if target is None:
                raise KmlParseError("KMZ archive contains no .kml entry")
            return archive.read(target)
    except zipfile.BadZipFile as exc:
        raise KmlParseError(f"invalid KMZ archive: {exc}") from exc


def read_placemarks(data: bytes) -> list[KmlPlacemark]:
    """Parse KML/KMZ bytes into placemarks with GeoJSON geometry dicts."""
    xml = _unwrap_kmz(data)
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise KmlParseError(f"KML could not be parsed: {exc}") from exc
    placemarks: list[KmlPlacemark] = []
    for pm in _descendants(root, "Placemark"):
        name_el = _child(pm, "name")
        desc_el = _child(pm, "description")
        geom_el = _geometry_element(pm)
        geometry = _convert_geometry(geom_el) if geom_el is not None else None
        placemarks.append(
            KmlPlacemark(
                name=name_el.text if name_el is not None else None,
                description=desc_el.text if desc_el is not None else None,
                extended_data=_extended_data(pm),
                geometry=geometry,
            )
        )
    return placemarks
