"""``GeoProjectionBackend`` — the rebuildable ``project.geo.db`` map sidecar.

Modeled on ``frisket.embeddings.vector_backend.VectorBackend``: a per-project
SQLite sidecar that is rebuildable from
``project.db`` + canonical live values, never in an atomic transaction with
``project.db``, safe to delete, and excluded from default bundle export. Raw
projected points live ONLY here; the canonical ``geo_point`` value stays in
``project.db``.

The seam is intentionally tiny: ``ensure_schema``,
``materialize_geo_column``, ``query_points``, ``projection_status``. No plugin
framework.

Freshness is lazy by generation hash:
the generation descriptor is hashed from canonical state (op cursor, the
column's latest applied generation, run/generation status, and the
``geo_point`` contract version). A projection whose ``generation_hash`` no
longer matches current
canonical state is never served as ready — it is rebuilt on read. This avoids
wiring invalidation hooks into every edit/run/undo path; correctness is
mechanical, not event-driven.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from frisket.authoring import column_types
from frisket.engine.projections.point_wire import MapPoint
from frisket.engine.store import Project
from frisket.engine.store.result_generations import ResultGenerationStore

# Bump GEO_POINT_CONTRACT when the geo_point wire shape or coordinate validity
# rules change so every cached projection rebuilds.
GEO_POINT_CONTRACT = "geo_point.v1"

# Bump GEO_SHAPE_CONTRACT when the geo_shape wire shape or geometry validity
# rules change so every cached shape projection rebuilds.
GEO_SHAPE_CONTRACT = "geo_shape.v1"

GEO_SIDECAR_FILENAME = "project.geo.db"

# A plugin-domain projection-kind identifier, not host-generic infrastructure
# -- it names the specific projection kind the `frisket.geo` plugin declares
# for the role=`map_points` runtime binding that `server/services/map_points.py`
# looks up generically by metadata role, never by this literal string.
MAP_POINTS_PROJECTION_KIND = "frisket.geo.projection.map_points"

GEO_SIDECAR_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS geo_projection_meta (
  projection_id TEXT PRIMARY KEY,
  sheet_id INTEGER NOT NULL,
  column_id INTEGER NOT NULL,
  generation_hash TEXT NOT NULL,
  generation_json TEXT NOT NULL,
  backend_id TEXT NOT NULL,
  backend_version TEXT NOT NULL,
  status TEXT NOT NULL,
  transient INTEGER NOT NULL DEFAULT 0,
  total_rows INTEGER NOT NULL DEFAULT 0,
  valid_points INTEGER NOT NULL DEFAULT 0,
  invalid_points INTEGER NOT NULL DEFAULT 0,
  built_at TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(sheet_id, column_id, generation_hash)
);

CREATE TABLE IF NOT EXISTS geo_points (
  point_id INTEGER PRIMARY KEY,
  projection_id TEXT NOT NULL REFERENCES geo_projection_meta(projection_id)
    ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  lon REAL NOT NULL,
  lat REAL NOT NULL,
  value_ref_json TEXT NOT NULL,
  attr_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(projection_id, row_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS geo_points_rtree USING rtree(
  point_id,
  min_lon,
  max_lon,
  min_lat,
  max_lat
);

CREATE INDEX IF NOT EXISTS idx_geo_points_projection_row
  ON geo_points(projection_id, row_id);

CREATE TABLE IF NOT EXISTS geo_shapes (
  shape_id INTEGER PRIMARY KEY,
  projection_id TEXT NOT NULL REFERENCES geo_projection_meta(projection_id)
    ON DELETE CASCADE,
  row_id INTEGER NOT NULL,
  geometry_json TEXT NOT NULL,
  UNIQUE(projection_id, row_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS geo_shapes_rtree USING rtree(
  shape_id,
  min_lon,
  max_lon,
  min_lat,
  max_lat
);

CREATE INDEX IF NOT EXISTS idx_geo_shapes_projection_row
  ON geo_shapes(projection_id, row_id);
"""

# Resolve live values in chunks so a huge sheet never builds one giant dict.
_MATERIALIZE_CHUNK = 1000

# Per-(sheet,column) build locks so two concurrent map requests do not both
# rebuild the same projection. Process-local, matching the local-tier model.
_build_locks_guard = threading.Lock()
_build_locks: dict[tuple[Path, int, int], threading.Lock] = {}

# Per-sidecar-file schema-creation locks. Schema creation is per-FILE (not per
# column), so the per-(sheet,column) build lock does not serialize it: concurrent
# first map requests across DIFFERENT columns would otherwise run executescript()
# on the same fresh project.geo.db at once and hit "database is locked".
_schema_locks_guard = threading.Lock()
_schema_locks: dict[Path, threading.Lock] = {}


class GeoProjectionError(Exception):
    """Typed projection error. ``code`` maps to the endpoint's typed HTTP error
    body so the frontend can distinguish 'not a geo column' from 'no sheet'."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _coerce_geo_point(value: Any) -> tuple[float, float] | None:
    """Return ``(lon, lat)`` for a valid ``geo_point`` value, else ``None``.

    Uses JSON decoding plus the registered ``geo_point`` validator — never
    ad-hoc string slicing. The validator
    enforces the dict shape and coordinate ranges; missing/empty/invalid values
    return ``None`` and are counted as invalid by the caller.
    """
    if value is None or value == "":
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(parsed, dict):
        return None
    if not column_types.validate_value("geo_point", parsed):
        return None
    lon = parsed.get("lon")
    lat = parsed.get("lat")
    return (float(lon), float(lat))


@dataclass(frozen=True)
class GeoShapeCandidate:
    """A bbox-prefiltered ``geo_shape`` candidate the spatial-join lane consumes:
    the canonical GeoJSON geometry plus its ``row_id``. The exact predicate
    (``shapely`` contains/covers/intersects) runs against ``geometry`` after this
    cheap R*Tree bbox prefilter narrows the pairs."""

    row_id: int
    geometry: dict[str, Any]


def _coerce_geo_shape(value: Any) -> dict[str, Any] | None:
    """Return a valid ``geo_shape`` GeoJSON geometry dict, else ``None``.

    Mirrors ``_coerce_geo_point``: JSON-decode when stored as text, then gate
    through the registered ``geo_shape`` validator (empty/invalid → ``None``,
    counted invalid by the caller).
    """
    if value is None or value == "":
        return None
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
    if not isinstance(parsed, dict):
        return None
    if not column_types.validate_value("geo_shape", parsed):
        return None
    return parsed


def _shape_bounds(geometry: dict[str, Any]) -> tuple[float, float, float, float]:
    """Return ``(min_lon, min_lat, max_lon, max_lat)`` for a GeoJSON geometry.

    Uses ``shapely.geometry.shape`` so the bbox
    matches the geometry engine the exact join predicate later runs on.
    """
    from shapely.geometry import shape as _shapely_shape

    min_lon, min_lat, max_lon, max_lat = _shapely_shape(geometry).bounds
    return (float(min_lon), float(min_lat), float(max_lon), float(max_lat))


class GeoProjectionBackend:
    """Per-project rebuildable map projection store at ``project.geo.db``.

    Open it, then call ``materialize_geo_column`` / ``query_points``; both lazily
    (re)build the projection when the canonical generation hash changes.
    """

    BACKEND_ID = "sqlite-rtree"
    BACKEND_VERSION = "1"

    def __init__(self, project: Project):
        self.project = project
        self.db_path = project.path / GEO_SIDECAR_FILENAME
        self._conn: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._conn is None:
            # isolation_level=None => autocommit, so the explicit BEGIN/COMMIT
            # blocks in _build/_prune are the only transactions (no implicit
            # nested-transaction surprises).
            conn = sqlite3.connect(
                self.db_path, check_same_thread=False, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._conn = conn
        return self._conn

    def _schema_lock(self) -> threading.Lock:
        with _schema_locks_guard:
            lock = _schema_locks.get(self.db_path)
            if lock is None:
                lock = threading.Lock()
                _schema_locks[self.db_path] = lock
            return lock

    def ensure_schema(self) -> None:
        # Serialize per sidecar file: concurrent first-time creation across
        # columns/connections otherwise races executescript() → "database is
        # locked" (idempotent CREATE IF NOT EXISTS on later calls is cheap).
        with self._schema_lock():
            self.db.executescript(GEO_SIDECAR_SCHEMA)
            self.db.commit()

    # -- canonical column resolution -------------------------------------

    def _require_geo_column(self, sheet_id: int, column_id: int) -> sqlite3.Row:
        """Validate the (sheet, column) is a visible ``geo_point`` column.

        Raises typed ``GeoProjectionError`` so the endpoint returns a precise
        400/404, never a blank map or a 500.
        """
        visible_sheets = {s["id"] for s in self.project.sheets(include_hidden=False)}
        if sheet_id not in visible_sheets:
            raise GeoProjectionError("sheet_not_found", f"no visible sheet {sheet_id}")
        col = self.project.get_column(column_id)
        if col is None or col["sheet_id"] != sheet_id:
            raise GeoProjectionError(
                "column_not_found", f"no column {column_id} on sheet {sheet_id}"
            )
        if col["hidden"]:
            raise GeoProjectionError(
                "column_not_found", f"column {column_id} is hidden"
            )
        if col["type"] != "geo_point":
            raise GeoProjectionError(
                "not_geo_point",
                f"column {column_id} is type '{col['type']}', not geo_point",
            )
        return col

    def _require_geo_shape_column(self, sheet_id: int, column_id: int) -> sqlite3.Row:
        """Validate the (sheet, column) is a visible ``geo_shape`` column.

        Symmetric to ``_require_geo_column`` so the shape sidecar returns precise
        typed errors instead of a 500 or an empty result.
        """
        visible_sheets = {s["id"] for s in self.project.sheets(include_hidden=False)}
        if sheet_id not in visible_sheets:
            raise GeoProjectionError("sheet_not_found", f"no visible sheet {sheet_id}")
        col = self.project.get_column(column_id)
        if col is None or col["sheet_id"] != sheet_id:
            raise GeoProjectionError(
                "column_not_found", f"no column {column_id} on sheet {sheet_id}"
            )
        if col["hidden"]:
            raise GeoProjectionError(
                "column_not_found", f"column {column_id} is hidden"
            )
        if col["type"] != "geo_shape":
            raise GeoProjectionError(
                "not_geo_shape",
                f"column {column_id} is type '{col['type']}', not geo_shape",
            )
        return col

    def _run_state(self, run_id: int | None, column_id: int) -> dict[str, Any] | None:
        if run_id is None:
            return None
        run = self.project.db.execute(
            "SELECT status, total_rows, completed_rows, failed_rows "
            "FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run is None:
            return None
        binding = ResultGenerationStore(self.project).get_binding(run_id, column_id)
        return {
            "status": run["status"],
            "total_rows": run["total_rows"],
            "completed_rows": run["completed_rows"],
            "failed_rows": run["failed_rows"],
            "generation_state": None if binding is None else binding.state,
        }

    def _generation(self, sheet_id: int, column_id: int, col: sqlite3.Row) -> dict:
        """Canonical generation descriptor. Hashing this decides freshness:
        op_cursor covers edits + undo/redo, the latest applied generation and
        its state cover runs/reruns/publication, and the contract version forces
        a rebuild if geo_point
        validity rules change."""
        run_id = ResultGenerationStore(self.project).latest_applied_run_id(column_id)
        run_state = self._run_state(run_id, column_id)
        descriptor = {
            "backend_id": self.BACKEND_ID,
            "backend_version": self.BACKEND_VERSION,
            "sheet_id": sheet_id,
            "column_id": column_id,
            "column_name": col["name"],
            "column_type": col["type"],
            "op_cursor": self.project.op_cursor,
            "latest_run_id": run_id,
            "run_state": run_state,
            "geo_point_contract": GEO_POINT_CONTRACT,
        }
        return descriptor

    def _generation_shape(
        self, sheet_id: int, column_id: int, col: sqlite3.Row
    ) -> dict:
        """Canonical generation descriptor for a ``geo_shape`` projection.

        Mirrors ``_generation`` but keys on ``GEO_SHAPE_CONTRACT`` (and its own
        projection_kind) so shape projections rebuild independently of points
        when the shape wire changes.
        """
        run_id = ResultGenerationStore(self.project).latest_applied_run_id(column_id)
        run_state = self._run_state(run_id, column_id)
        descriptor = {
            "projection_kind": "geo_shape",
            "backend_id": self.BACKEND_ID,
            "backend_version": self.BACKEND_VERSION,
            "sheet_id": sheet_id,
            "column_id": column_id,
            "column_name": col["name"],
            "column_type": col["type"],
            "op_cursor": self.project.op_cursor,
            "latest_run_id": run_id,
            "run_state": run_state,
            "geo_shape_contract": GEO_SHAPE_CONTRACT,
        }
        return descriptor

    @staticmethod
    def _generation_hash(descriptor: dict) -> str:
        blob = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_transient(descriptor: dict) -> bool:
        """A projection over a still-running/queued current run is transient:
        results are still landing, so callers should poll rather than treat it
        as long-lived."""
        run_state = descriptor.get("run_state")
        if not run_state:
            return False
        return run_state.get("status") in {"running", "queued"}

    # -- materialization --------------------------------------------------

    def _build_lock(self, sheet_id: int, column_id: int) -> threading.Lock:
        key = (self.db_path, sheet_id, column_id)
        with _build_locks_guard:
            lock = _build_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                _build_locks[key] = lock
            return lock

    def materialize_geo_column(self, sheet_id: int, column_id: int) -> dict[str, Any]:
        """Ensure a ready projection for the current generation and return its
        status dict. Reuses a matching ready projection; otherwise rebuilds
        lazily and atomically (build under a fresh projection_id, then mark
        ready) so readers only ever see a complete projection."""
        with self._build_lock(sheet_id, column_id):
            # The lock covers schema creation as well as the build. Concurrent
            # first map requests otherwise race in executescript() before the
            # projection_id-level write path is reached.
            self.ensure_schema()
            col = self._require_geo_column(sheet_id, column_id)
            descriptor = self._generation(sheet_id, column_id, col)
            gen_hash = self._generation_hash(descriptor)

            existing = self.db.execute(
                "SELECT * FROM geo_projection_meta "
                "WHERE sheet_id=? AND column_id=? AND generation_hash=? "
                "AND status='ready'",
                (sheet_id, column_id, gen_hash),
            ).fetchone()
            if existing is not None:
                return self._status_dict(existing)
            return self._build(sheet_id, column_id, descriptor, gen_hash)

    def _build(
        self, sheet_id: int, column_id: int, descriptor: dict, gen_hash: str
    ) -> dict[str, Any]:
        projection_id = (
            "geoproj_" + hashlib.sha256(gen_hash.encode("utf-8")).hexdigest()[:24]
        )
        transient = self._is_transient(descriptor)
        gen_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))

        row_ids = self.project.visible_row_ids(sheet_id)
        valid = 0
        invalid = 0
        points: list[tuple[int, float, float, str]] = []
        for start in range(0, len(row_ids), _MATERIALIZE_CHUNK):
            chunk = row_ids[start : start + _MATERIALIZE_CHUNK]
            vals, refs = self.project.get_values_with_refs(
                sheet_id, column_id, row_ids=chunk
            )
            for rid in chunk:
                coords = _coerce_geo_point(vals.get(rid))
                if coords is None:
                    invalid += 1
                    continue
                lon, lat = coords
                ref = refs.get(rid, {"kind": "missing", "row_id": rid})
                points.append((rid, lon, lat, json.dumps(ref, sort_keys=True)))
                valid += 1

        conn = self.db
        try:
            # IMMEDIATE takes the write lock up front so busy_timeout serializes
            # concurrent sidecar writers; a deferred BEGIN would upgrade-deadlock
            # and return "database is locked" without honoring the timeout.
            conn.execute("BEGIN IMMEDIATE")
            # Clear any prior (possibly stale/partial) build for this id — rtree
            # rows are not FK children so drop them explicitly — then write the
            # whole projection in one transaction and only then mark it ready.
            leftover = [
                int(r["point_id"])
                for r in conn.execute(
                    "SELECT point_id FROM geo_points WHERE projection_id=?",
                    (projection_id,),
                )
            ]
            conn.executemany(
                "DELETE FROM geo_points_rtree WHERE point_id=?",
                [(i,) for i in leftover],
            )
            conn.execute(
                "DELETE FROM geo_projection_meta WHERE projection_id=?",
                (projection_id,),
            )
            conn.execute(
                "INSERT INTO geo_projection_meta "
                "(projection_id, sheet_id, column_id, generation_hash, "
                "generation_json, backend_id, backend_version, status, transient, "
                "total_rows, valid_points, invalid_points, built_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))",
                (
                    projection_id,
                    sheet_id,
                    column_id,
                    gen_hash,
                    gen_json,
                    self.BACKEND_ID,
                    self.BACKEND_VERSION,
                    "building",
                    1 if transient else 0,
                    len(row_ids),
                    valid,
                    invalid,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            for rid, lon, lat, ref_json in points:
                cur = conn.execute(
                    "INSERT INTO geo_points "
                    "(projection_id, row_id, lon, lat, value_ref_json) "
                    "VALUES (?,?,?,?,?)",
                    (projection_id, rid, lon, lat, ref_json),
                )
                point_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO geo_points_rtree "
                    "(point_id, min_lon, max_lon, min_lat, max_lat) "
                    "VALUES (?,?,?,?,?)",
                    (point_id, lon, lon, lat, lat),
                )
            conn.execute(
                "UPDATE geo_projection_meta SET status='ready' WHERE projection_id=?",
                (projection_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # Drop superseded generations for this (sheet,column); the current ready
        # projection is the only one we keep. R*Tree rows cascade via point
        # deletes below (rtree is not a real FK target, so clean explicitly).
        self._prune_other_generations(sheet_id, column_id, keep=projection_id)

        meta = self.db.execute(
            "SELECT * FROM geo_projection_meta WHERE projection_id=?",
            (projection_id,),
        ).fetchone()
        return self._status_dict(meta)

    def _prune_other_generations(
        self, sheet_id: int, column_id: int, *, keep: str
    ) -> None:
        stale = [
            r["projection_id"]
            for r in self.db.execute(
                "SELECT projection_id FROM geo_projection_meta "
                "WHERE sheet_id=? AND column_id=? AND projection_id!=?",
                (sheet_id, column_id, keep),
            )
        ]
        if not stale:
            return
        conn = self.db
        try:
            # IMMEDIATE takes the write lock up front so busy_timeout serializes
            # concurrent sidecar writers; a deferred BEGIN would upgrade-deadlock
            # and return "database is locked" without honoring the timeout.
            conn.execute("BEGIN IMMEDIATE")
            for pid in stale:
                point_ids = [
                    int(r["point_id"])
                    for r in conn.execute(
                        "SELECT point_id FROM geo_points WHERE projection_id=?",
                        (pid,),
                    )
                ]
                conn.executemany(
                    "DELETE FROM geo_points_rtree WHERE point_id=?",
                    [(i,) for i in point_ids],
                )
                conn.execute("DELETE FROM geo_points WHERE projection_id=?", (pid,))
                conn.execute(
                    "DELETE FROM geo_projection_meta WHERE projection_id=?", (pid,)
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- geo_shape materialization ----------------------------------------

    def materialize_geo_shape_column(
        self, sheet_id: int, column_id: int
    ) -> dict[str, Any]:
        """Ensure a ready ``geo_shapes`` projection for the current generation.

        Mirrors ``materialize_geo_column``: reuses a matching ready projection,
        else rebuilds lazily and atomically under a fresh projection_id.
        """
        with self._build_lock(sheet_id, column_id):
            self.ensure_schema()
            col = self._require_geo_shape_column(sheet_id, column_id)
            descriptor = self._generation_shape(sheet_id, column_id, col)
            gen_hash = self._generation_hash(descriptor)

            existing = self.db.execute(
                "SELECT * FROM geo_projection_meta "
                "WHERE sheet_id=? AND column_id=? AND generation_hash=? "
                "AND status='ready'",
                (sheet_id, column_id, gen_hash),
            ).fetchone()
            if existing is not None:
                return self._status_dict(existing)
            return self._build_shape(sheet_id, column_id, descriptor, gen_hash)

    def _build_shape(
        self, sheet_id: int, column_id: int, descriptor: dict, gen_hash: str
    ) -> dict[str, Any]:
        projection_id = (
            "geoshape_" + hashlib.sha256(gen_hash.encode("utf-8")).hexdigest()[:24]
        )
        transient = self._is_transient(descriptor)
        gen_json = json.dumps(descriptor, sort_keys=True, separators=(",", ":"))

        row_ids = self.project.visible_row_ids(sheet_id)
        valid = 0
        invalid = 0
        # (row_id, geometry_json, min_lon, min_lat, max_lon, max_lat)
        shapes: list[tuple[int, str, float, float, float, float]] = []
        for start in range(0, len(row_ids), _MATERIALIZE_CHUNK):
            chunk = row_ids[start : start + _MATERIALIZE_CHUNK]
            vals, _refs = self.project.get_values_with_refs(
                sheet_id, column_id, row_ids=chunk
            )
            for rid in chunk:
                geometry = _coerce_geo_shape(vals.get(rid))
                if geometry is None:
                    invalid += 1
                    continue
                min_lon, min_lat, max_lon, max_lat = _shape_bounds(geometry)
                shapes.append(
                    (
                        rid,
                        json.dumps(geometry, sort_keys=True, separators=(",", ":")),
                        min_lon,
                        min_lat,
                        max_lon,
                        max_lat,
                    )
                )
                valid += 1

        conn = self.db
        try:
            conn.execute("BEGIN IMMEDIATE")
            leftover = [
                int(r["shape_id"])
                for r in conn.execute(
                    "SELECT shape_id FROM geo_shapes WHERE projection_id=?",
                    (projection_id,),
                )
            ]
            conn.executemany(
                "DELETE FROM geo_shapes_rtree WHERE shape_id=?",
                [(i,) for i in leftover],
            )
            conn.execute(
                "DELETE FROM geo_projection_meta WHERE projection_id=?",
                (projection_id,),
            )
            conn.execute(
                "INSERT INTO geo_projection_meta "
                "(projection_id, sheet_id, column_id, generation_hash, "
                "generation_json, backend_id, backend_version, status, transient, "
                "total_rows, valid_points, invalid_points, built_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))",
                (
                    projection_id,
                    sheet_id,
                    column_id,
                    gen_hash,
                    gen_json,
                    self.BACKEND_ID,
                    self.BACKEND_VERSION,
                    "building",
                    1 if transient else 0,
                    len(row_ids),
                    valid,
                    invalid,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ),
            )
            for rid, geometry_json, min_lon, min_lat, max_lon, max_lat in shapes:
                cur = conn.execute(
                    "INSERT INTO geo_shapes "
                    "(projection_id, row_id, geometry_json) VALUES (?,?,?)",
                    (projection_id, rid, geometry_json),
                )
                shape_id = int(cur.lastrowid)
                conn.execute(
                    "INSERT INTO geo_shapes_rtree "
                    "(shape_id, min_lon, max_lon, min_lat, max_lat) "
                    "VALUES (?,?,?,?,?)",
                    (shape_id, min_lon, max_lon, min_lat, max_lat),
                )
            conn.execute(
                "UPDATE geo_projection_meta SET status='ready' WHERE projection_id=?",
                (projection_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        self._prune_other_shape_generations(sheet_id, column_id, keep=projection_id)

        meta = self.db.execute(
            "SELECT * FROM geo_projection_meta WHERE projection_id=?",
            (projection_id,),
        ).fetchone()
        return self._status_dict(meta)

    def _prune_other_shape_generations(
        self, sheet_id: int, column_id: int, *, keep: str
    ) -> None:
        stale = [
            r["projection_id"]
            for r in self.db.execute(
                "SELECT projection_id FROM geo_projection_meta "
                "WHERE sheet_id=? AND column_id=? AND projection_id!=?",
                (sheet_id, column_id, keep),
            )
        ]
        if not stale:
            return
        conn = self.db
        try:
            conn.execute("BEGIN IMMEDIATE")
            for pid in stale:
                shape_ids = [
                    int(r["shape_id"])
                    for r in conn.execute(
                        "SELECT shape_id FROM geo_shapes WHERE projection_id=?",
                        (pid,),
                    )
                ]
                conn.executemany(
                    "DELETE FROM geo_shapes_rtree WHERE shape_id=?",
                    [(i,) for i in shape_ids],
                )
                conn.execute("DELETE FROM geo_shapes WHERE projection_id=?", (pid,))
                conn.execute(
                    "DELETE FROM geo_projection_meta WHERE projection_id=?", (pid,)
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # -- query ------------------------------------------------------------

    def query_shapes(
        self,
        sheet_id: int,
        column_id: int,
        *,
        row_ids: list[int] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> tuple[list[GeoShapeCandidate], dict[str, Any]]:
        """Return (candidates, projection_status). Lazily (re)materializes first.

        Symmetric to ``query_points``: ``bbox`` is
        ``(min_lon, min_lat, max_lon, max_lat)`` and prefilters through the
        R*Tree (classic overlap predicate), ``row_ids`` restricts to a set. Each
        candidate carries the canonical GeoJSON geometry so the join lane can run
        the exact ``shapely`` predicate; results are in stable projection order.
        """
        status = self.materialize_geo_shape_column(sheet_id, column_id)
        projection_id = status["projection_id"]

        params: list[Any] = [projection_id]
        sql = (
            "SELECT s.row_id AS row_id, s.geometry_json AS geometry_json "
            "FROM geo_shapes s "
        )
        where = ["s.projection_id=?"]
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            sql += "JOIN geo_shapes_rtree r ON r.shape_id = s.shape_id "
            where.append(
                "r.min_lon<=? AND r.max_lon>=? AND r.min_lat<=? AND r.max_lat>=?"
            )
            params.extend([max_lon, min_lon, max_lat, min_lat])
        if row_ids is not None:
            if not row_ids:
                return [], status
            placeholders = ",".join("?" * len(row_ids))
            where.append(f"s.row_id IN ({placeholders})")
            params.extend(int(r) for r in row_ids)
        sql += "WHERE " + " AND ".join(where) + " ORDER BY s.shape_id"

        rows = self.db.execute(sql, params).fetchall()
        candidates = [
            GeoShapeCandidate(
                row_id=int(r["row_id"]),
                geometry=json.loads(r["geometry_json"]),
            )
            for r in rows
        ]
        return candidates, status

    def query_points(
        self,
        sheet_id: int,
        column_id: int,
        *,
        row_ids: list[int] | None = None,
        bbox: tuple[float, float, float, float] | None = None,
    ) -> tuple[list[MapPoint], dict[str, Any]]:
        """Return (points, projection_status). Lazily (re)materializes first.

        ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)`` and filters through
        the R*Tree. ``row_ids``, when given, restricts to that set (the caller
        resolves filtered/sorted row ids via the shared sheet filter helpers).
        Points are returned in stable insertion order (sheet position order) so
        deck.gl picking indexes are deterministic.
        """
        status = self.materialize_geo_column(sheet_id, column_id)
        projection_id = status["projection_id"]

        params: list[Any] = [projection_id]
        sql = "SELECT p.row_id AS row_id, p.lon AS lon, p.lat AS lat FROM geo_points p "
        where = ["p.projection_id=?"]
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = bbox
            sql += "JOIN geo_points_rtree r ON r.point_id = p.point_id "
            where.append(
                "r.min_lon<=? AND r.max_lon>=? AND r.min_lat<=? AND r.max_lat>=?"
            )
            params.extend([max_lon, min_lon, max_lat, min_lat])
        if row_ids is not None:
            if not row_ids:
                return [], status
            placeholders = ",".join("?" * len(row_ids))
            where.append(f"p.row_id IN ({placeholders})")
            params.extend(int(r) for r in row_ids)
        sql += "WHERE " + " AND ".join(where) + " ORDER BY p.point_id"

        rows = self.db.execute(sql, params).fetchall()
        points = [
            MapPoint(row_id=int(r["row_id"]), lon=float(r["lon"]), lat=float(r["lat"]))
            for r in rows
        ]
        return points, status

    def projection_status(self, sheet_id: int, column_id: int) -> dict[str, Any]:
        """Materialize if needed and return the current projection status."""
        return self.materialize_geo_column(sheet_id, column_id)

    def _status_dict(self, meta: sqlite3.Row) -> dict[str, Any]:
        return {
            "projection_id": meta["projection_id"],
            "sheet_id": meta["sheet_id"],
            "column_id": meta["column_id"],
            "generation_hash": meta["generation_hash"],
            "backend_id": meta["backend_id"],
            "backend_version": meta["backend_version"],
            "status": meta["status"],
            "transient": bool(meta["transient"]),
            "total_rows": meta["total_rows"],
            "valid_points": meta["valid_points"],
            "invalid_points": meta["invalid_points"],
            "built_at": meta["built_at"],
        }

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
