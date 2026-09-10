"""Media column detection and fixed reference-column projection.

Row-shaped dataset exports never carry blob bytes, base64, or local filesystem
paths. A media column flattens into a stable set of sibling reference columns so
CSV headers stay constant and Parquet schemas stay predictable.
"""

from __future__ import annotations

from typing import Any

from frisket.server.exports.plan import ExportColumn
from frisket.server.exports.rowset import ExportError
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import MediaBlobStore

# Registered column types whose cells are media (see frisket.column_types).
MEDIA_COLUMN_TYPES = frozenset({"image", "audio", "video", "file"})

# Fixed sibling suffixes appended after the base media display column, in order.
# ``media`` (base display) + these nine produce the contract's stable schema.
MEDIA_SIBLING_FIELDS: tuple[str, ...] = (
    "blob",
    "mime_type",
    "filename",
    "size_bytes",
    "source_url",
    "duration_seconds",
    "width",
    "height",
    "pages",
)

_SIBLING_TYPE = {
    "blob": "text",
    "mime_type": "text",
    "filename": "text",
    "size_bytes": "integer",
    "source_url": "text",
    "duration_seconds": "number",
    "width": "integer",
    "height": "integer",
    "pages": "integer",
}


def is_media_column(column_type: str) -> bool:
    return column_type in MEDIA_COLUMN_TYPES


def project_export_columns(
    project: Project, sheet_id: int, media_policy: str
) -> list[ExportColumn]:
    """Project visible sheet columns into output columns.

    Non-media columns pass through as value columns. Under
    ``media_policy='references'`` a media column expands into a base display
    column plus the fixed sibling reference columns; under ``'omit'`` the media
    column and all its siblings are dropped. A generated sibling whose name
    collides with another export column fails loudly.
    """
    out: list[ExportColumn] = []
    for column in project.columns(sheet_id):
        name = str(column["name"])
        ctype = str(column["type"])
        col_id = int(column["id"])
        if is_media_column(ctype):
            if media_policy == "omit":
                continue
            out.append(
                ExportColumn(
                    name=name,
                    type=ctype,
                    role="media_display",
                    source_column_id=col_id,
                    column_id=col_id,
                    media_field="display",
                )
            )
            for field in MEDIA_SIBLING_FIELDS:
                out.append(
                    ExportColumn(
                        name=f"{name}.{field}",
                        type=_SIBLING_TYPE[field],
                        role="media_ref",
                        source_column_id=col_id,
                        column_id=None,
                        media_field=field,
                    )
                )
        else:
            out.append(
                ExportColumn(
                    name=name,
                    type=ctype,
                    role="value",
                    source_column_id=col_id,
                    column_id=col_id,
                )
            )
    _reject_collisions(out)
    return out


def _reject_collisions(columns: list[ExportColumn]) -> None:
    seen: dict[str, ExportColumn] = {}
    for column in columns:
        prior = seen.get(column.name)
        if prior is not None:
            raise ExportError(
                "export_column_collision",
                f"export column name {column.name!r} is produced by more than one "
                "column; media reference flattening would create a duplicate header",
                field="params.media_policy",
                details={
                    "name": column.name,
                    "roles": sorted({prior.role, column.role}),
                },
            )
        seen[column.name] = column


def resolve_media_reference(project: Project, value: Any) -> dict[str, Any]:
    """Resolve the display value and reference siblings for one media cell.

    Identity comes from the cell and ``blobs`` row; display facts come from the
    blob's owned metadata namespaces. Never returns bytes or a filesystem path.
    """
    envelope: dict[str, Any] = value if isinstance(value, dict) else {}
    blob_digest = envelope.get("blob") if isinstance(value, dict) else None
    cell_source_url = envelope.get("source_url")
    if isinstance(value, str) and value:
        cell_source_url = value

    blob_row: Any = None
    display_meta: dict[str, Any] = {}
    if isinstance(blob_digest, str) and blob_digest:
        blob_row = project.db.execute(
            "SELECT filename, mime, size, source_url, metadata FROM blobs WHERE hash=?",
            (blob_digest,),
        ).fetchone()
        if blob_row is not None:
            display_meta = MediaBlobStore(project).display_metadata(blob_digest)

    def blob_field(key: str) -> Any:
        return blob_row[key] if blob_row is not None else None

    mime_type = _pick(envelope.get("mime"), blob_field("mime"))
    filename = _pick(envelope.get("filename"), blob_field("filename"))
    size_bytes = blob_field("size")
    source_url = _pick(cell_source_url, blob_field("source_url"))
    duration = display_meta.get("duration_seconds")
    width = display_meta.get("width")
    height = display_meta.get("height")
    pages = display_meta.get("pages")

    blob_prefix = (
        blob_digest[:12] if isinstance(blob_digest, str) and blob_digest else None
    )
    display = _pick(filename, source_url, blob_prefix)

    return {
        "display": display,
        "blob": blob_digest if isinstance(blob_digest, str) else None,
        "mime_type": mime_type,
        "filename": filename,
        "size_bytes": size_bytes,
        "source_url": source_url,
        "duration_seconds": duration,
        "width": width,
        "height": height,
        "pages": pages,
    }


def _pick(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None
