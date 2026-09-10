"""export.column_tables: explode a JSON list column into a zip of per-table CSVs.

Shares the generic single-artifact local-file
delivery helpers (`_deliver_export_artifact` et al.) and the project-write
error/notification seams with `frisket.executor.action_families.exports`;
lives in its own module because the shape-detection/rendering logic below is
sizeable and orthogonal to the hand-written CSV/JSONL/Parquet/FollowTheMoney
exports there.

Shape handling is declarative, with no expression strings. A source column
cell is a JSON list whose items are uniformly
one of three shapes across the WHOLE column:

* ``dict`` -- list-of-dicts (pdf_tables' extracted-table rows are the
  primary case). The CSV header for a given (row, group) artifact is the
  union of keys across that group's own items (first-seen order), minus
  ``group_by`` (always excluded -- its value is already encoded by the
  split + manifest) and any ``exclude_columns``.
* ``list`` -- list-of-single-depth-lists (headerless raw rows). Columns are
  synthesized as ``column_1``/``column_2``/... unless ``first_row_header``
  treats the group's first item as the header row. ``group_by`` and
  ``exclude_columns`` do not apply (no field names to group/exclude by).
* ``scalar`` -- a plain list of scalars. Renders as a single ``value``
  column. ``group_by``/``exclude_columns`` do not apply.

``group_by`` (dict shape only) splits each row's items into one artifact per
distinct value (first-appearance order); omitting it yields one artifact per
row (a single implicit group). Filenames render through
``frisket.output_names.format_output_name`` against ``{row, table, column,
source_stem?}`` (``source_stem`` is present only when an item in the group
carries a ``source_filename`` string field -- the pdf_tables convention) and
are deduped via ``dedupe_output_name`` (deterministic ``-2``/``-3`` suffixes,
never a silent overwrite).
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from frisket.actions.core import _ProjectAction
from frisket.actions.exports import ExportColumnTablesParams
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ColumnTablesDestination,
    ColumnTablesExport,
    ColumnTablesExporter,
)
from frisket.contracts.action import (
    ActionError,
    ActionIdentity,
    ActionOutput,
    ActionResult,
    Receipt,
    ReceiptEvidence,
    ReceiptIO,
)
from frisket.engine.executor.action_families.exports import (
    _deliver_export_artifact,
    _emit_export_notification,
    _export_project_write_failed_error,
    _ExportDelivery,
    _finalize_export_delivery,
    _precheck_export_package_destination,
    _rollback_export_delivery,
)
from frisket.engine.executor.action_inventory import (
    ExecutorContext,
    ExecutorDeps,
    _ActionCoreSpec,
    _TypedProjectEnvelope,
)
from frisket.engine.executor.action_lifecycle import (
    _child_sheet_deterministic_result_from_existing,
    _run_action_core_spec,
)
from frisket.engine.executor.map_rows_action import typed_request_hash
from frisket.server.exports.sheet_csv import csv_display
from frisket.authoring.output_names import (
    OutputNameTemplateError,
    dedupe_output_name,
    format_output_name,
    sanitize_output_name,
    source_stem,
)
from frisket.engine.store import Project
from frisket.engine.store.blob_backend import BlobIntegrityError, BlobNotFoundError
from frisket.engine.store.media_blobs import MediaBlobStore
from frisket.engine.store.receipts import ReceiptStore


ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def _column_tables_error(
    code: str,
    message: str,
    *,
    field: str | None = None,
    details: dict[str, Any] | None = None,
) -> ActionError:
    return ActionError(
        code=code,
        message=f"export.column_tables {message}",
        action_kind="export.column_tables",
        field=field,
        details=details or {},
    )


@dataclass
class _ColumnArtifact:
    filename: str
    content: bytes
    row_id: int
    source_filename: str | None
    source_blob_hash: str | None
    group_value: Any
    columns: list[str]
    entry_count: int


@dataclass
class _ColumnTablesResolved:
    destination_kind: str
    artifact_ref: dict[str, Any]
    manifest: dict[str, Any]
    content: bytes = field(repr=False)
    package_filename: str = ""
    tmp_path: Path | None = None
    destination: Path | None = None
    project_file_prefix: str | None = None
    delivery: _ExportDelivery | None = None


# ---------- shape detection + grouping + rendering ----------


def _detect_shape(
    rows_with_items: list[tuple[int, list[Any]]],
) -> tuple[str, list[str]] | ActionError:
    has_dict = has_list = has_scalar = False
    union_order: list[str] = []
    seen: set[str] = set()
    for _row_id, items in rows_with_items:
        for item in items:
            if isinstance(item, dict):
                has_dict = True
                for key in item:
                    if key not in seen:
                        seen.add(key)
                        union_order.append(key)
            elif isinstance(item, list):
                has_list = True
            else:
                has_scalar = True
    kinds = [
        k
        for k, present in (
            ("dict", has_dict),
            ("list", has_list),
            ("scalar", has_scalar),
        )
        if present
    ]
    if len(kinds) > 1:
        return _column_tables_error(
            "column_shape_mismatch",
            "source column list items must be uniformly objects, lists, or scalars",
            field="params.column_id",
            details={"shapes_found": sorted(kinds)},
        )
    return kinds[0], union_order


def _group_items(
    items: list[Any], *, group_by: str | None
) -> list[tuple[Any, list[Any]]]:
    if group_by is None:
        return [(None, items)]
    order: list[tuple[Any, Any]] = []
    buckets: dict[Any, list[Any]] = {}
    for item in items:
        value = item.get(group_by) if isinstance(item, dict) else None
        try:
            hash(value)
            bucket_key: Any = value
        except TypeError:
            bucket_key = json.dumps(value, sort_keys=True, default=str)
        if bucket_key not in buckets:
            order.append((bucket_key, value))
            buckets[bucket_key] = []
        buckets[bucket_key].append(item)
    return [(value, buckets[bucket_key]) for bucket_key, value in order]


def _render_group(
    shape: str,
    group_items: list[Any],
    *,
    group_by: str | None,
    exclude: set[str],
    first_row_header: bool,
) -> tuple[list[str], list[list[str]]] | ActionError:
    if shape == "dict":
        union_order: list[str] = []
        seen: set[str] = set()
        for item in group_items:
            for key in item:
                if key not in seen:
                    seen.add(key)
                    union_order.append(key)
        header = [key for key in union_order if key != group_by and key not in exclude]
        if not header:
            return _column_tables_error(
                "empty_output_columns",
                "group_by/exclude_columns removed every output column",
                field="params.exclude_columns",
            )
        body = [[csv_display(item.get(key)) for key in header] for item in group_items]
        return header, body

    if shape == "list":
        widths = {len(item) for item in group_items if isinstance(item, list)}
        if len(widths) > 1:
            return _column_tables_error(
                "column_shape_mismatch",
                "list-of-lists items in one group have inconsistent widths",
                field="params.column_id",
                details={"widths_found": sorted(widths)},
            )
        width = next(iter(widths), 0)
        if first_row_header and group_items:
            header_item = group_items[0]
            header = [
                str(value) if value not in (None, "") else f"column_{index + 1}"
                for index, value in enumerate(header_item)
            ]
            data_items = group_items[1:]
        else:
            header = [f"column_{index + 1}" for index in range(width)]
            data_items = group_items
        body = [[csv_display(value) for value in item] for item in data_items]
        return header, body

    # scalar
    header = ["value"]
    body = [[csv_display(item)] for item in group_items]
    return header, body


def _csv_bytes(header: list[str], body: list[list[str]]) -> bytes:
    import csv

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(body)
    return buffer.getvalue().encode("utf-8")


_KNOWN_SOURCE_FILENAME_KEYS = ("source_filename",)
_KNOWN_SOURCE_BLOB_HASH_KEYS = ("source_blob_hash",)


def _item_metadata_value(item: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(item, dict):
        return None
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_export_column_tables(
    project: Project, params: ExportColumnTablesParams, *, project_id: str
) -> _ColumnTablesResolved | ActionError:
    # Freeze all source facts together; ZIP rendering is deliberately eager,
    # but it must not mix a sheet/column lookup with a later row/value revision.
    with project.read_snapshot() as snapshot:
        sheet = snapshot.db.execute(
            "SELECT * FROM sheets WHERE id=? AND hidden=0", (params.sheet_id,)
        ).fetchone()
        if sheet is None:
            return _column_tables_error(
                "invalid_sheet_ref",
                "source sheet does not exist",
                field="params.sheet_id",
            )
        column = snapshot.db.execute(
            "SELECT * FROM columns WHERE id=? AND sheet_id=?",
            (params.column_id, params.sheet_id),
        ).fetchone()
        if column is None:
            return _column_tables_error(
                "invalid_column_ref",
                "source column does not exist on the source sheet",
                field="params.column_id",
            )
        if column["type"] != "json":
            return _column_tables_error(
                "invalid_column_ref",
                "source column must be a JSON list column",
                field="params.column_id",
            )
        column_name = str(column["name"])
        row_ids = snapshot.visible_row_ids(params.sheet_id)
        values = snapshot.get_values(params.sheet_id, params.column_id)

    rows_with_items: list[tuple[int, list[Any]]] = []
    for row_id in row_ids:
        cell = values.get(row_id)
        if cell is None:
            continue
        if not isinstance(cell, list):
            return _column_tables_error(
                "source_not_list",
                "source column cell must be a list",
                field="params.column_id",
                details={"row_id": row_id},
            )
        if cell:
            rows_with_items.append((int(row_id), cell))

    if not rows_with_items:
        return _column_tables_error(
            "empty_source_column",
            "source column has no list items to export",
            field="params.column_id",
        )

    shape_or_error = _detect_shape(rows_with_items)
    if isinstance(shape_or_error, ActionError):
        return shape_or_error
    shape, union_keys = shape_or_error

    group_by = params.group_by
    exclude_columns = set(params.exclude_columns or [])
    if shape != "dict":
        if group_by is not None:
            return _column_tables_error(
                "invalid_group_by",
                "group_by requires list-of-dicts items",
                field="params.group_by",
            )
        if exclude_columns:
            return _column_tables_error(
                "invalid_exclude_columns",
                "exclude_columns requires list-of-dicts items",
                field="params.exclude_columns",
            )
    else:
        if group_by is not None and group_by not in union_keys:
            return _column_tables_error(
                "invalid_group_by",
                "group_by key is absent from the list-item union of fields",
                field="params.group_by",
                details={"group_by": group_by, "available": union_keys},
            )
        unknown_excludes = sorted(
            name for name in exclude_columns if name not in union_keys
        )
        if unknown_excludes:
            return _column_tables_error(
                "invalid_exclude_columns",
                "exclude_columns names keys absent from the list-item union",
                field="params.exclude_columns",
                details={"unknown": unknown_excludes, "available": union_keys},
            )

    if params.first_row_header and shape != "list":
        return _column_tables_error(
            "invalid_params",
            "first_row_header only applies to list-of-lists items",
            field="params.first_row_header",
        )

    artifacts: list[_ColumnArtifact] = []
    used_names: set[str] = set()
    for row_ordinal, (row_id, items) in enumerate(rows_with_items):
        groups = _group_items(items, group_by=group_by if shape == "dict" else None)
        for table_ordinal, (group_value, group_items) in enumerate(groups):
            rendered = _render_group(
                shape,
                group_items,
                group_by=group_by,
                exclude=exclude_columns,
                first_row_header=params.first_row_header,
            )
            if isinstance(rendered, ActionError):
                return rendered
            header, body = rendered
            content = _csv_bytes(header, body)

            context: dict[str, Any] = {
                "row": row_ordinal,
                "table": table_ordinal,
                "column": column_name,
            }
            source_filename: str | None = None
            source_blob_hash: str | None = None
            if shape == "dict":
                for item in group_items:
                    if source_filename is None:
                        source_filename = _item_metadata_value(
                            item, _KNOWN_SOURCE_FILENAME_KEYS
                        )
                    if source_blob_hash is None:
                        source_blob_hash = _item_metadata_value(
                            item, _KNOWN_SOURCE_BLOB_HASH_KEYS
                        )
                    if source_filename is not None and source_blob_hash is not None:
                        break
            if source_filename is not None:
                context["source_stem"] = source_stem(source_filename)

            try:
                filename = format_output_name(params.name_template, context)
            except OutputNameTemplateError as exc:
                return _column_tables_error(
                    "invalid_name_template",
                    str(exc),
                    field="params.name_template",
                    details={"token": exc.token} if exc.token else {},
                )
            filename = dedupe_output_name(filename, used_names)
            used_names.add(filename)

            artifacts.append(
                _ColumnArtifact(
                    filename=filename,
                    content=content,
                    row_id=row_id,
                    source_filename=source_filename,
                    source_blob_hash=source_blob_hash,
                    group_value=group_value,
                    columns=header,
                    entry_count=len(group_items),
                )
            )

    manifest_artifacts = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.content).hexdigest()
        manifest_artifacts.append(
            {
                "filename": artifact.filename,
                "source_row_ref": {
                    "sheet_id": params.sheet_id,
                    "row_id": artifact.row_id,
                },
                "source_filename": artifact.source_filename,
                "source_blob_hash": artifact.source_blob_hash,
                "group_key": group_by,
                "group_value": artifact.group_value,
                "entry_count": artifact.entry_count,
                "columns": artifact.columns,
                "byte_count": len(artifact.content),
                "sha256": "sha256:" + digest,
            }
        )
    manifest = {
        "kind": "column_tables_export_manifest",
        "sheet_id": params.sheet_id,
        "column_id": params.column_id,
        "column_name": column_name,
        "shape": shape,
        "group_by": group_by,
        "name_template": params.name_template,
        "artifact_count": len(artifacts),
        "entry_count": sum(a.entry_count for a in artifacts),
        "artifacts": manifest_artifacts,
    }

    zip_bytes = _build_zip(artifacts, manifest)
    zip_sha256 = "sha256:" + hashlib.sha256(zip_bytes).hexdigest()
    package_filename = sanitize_output_name(f"{column_name}.zip")

    artifact_ref: dict[str, Any] = {
        "kind": "export_artifact",
        "export_kind": "column_tables",
        "format": "zip",
        "content_type": "application/zip",
        "byte_count": len(zip_bytes),
        "sha256": zip_sha256,
        "sheet_id": params.sheet_id,
        "column_id": params.column_id,
        "column_name": column_name,
        "shape": shape,
        "group_by": group_by,
        "artifact_count": len(artifacts),
        "entry_count": manifest["entry_count"],
    }

    if params.destination.kind == "local_dir":
        destination_dir = Path(params.destination.path)
        destination_error = _precheck_export_package_destination(
            destination_dir, action_kind="export.column_tables"
        )
        if destination_error is not None:
            return destination_error
        destination = destination_dir / package_filename
        tmp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp_path.write_bytes(zip_bytes)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            return _column_tables_error(
                "invalid_export_destination",
                "could not write the destination zip artifact",
                field="params.destination.path",
                details={"error": str(exc), "path": str(destination)},
            )
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
        artifact_ref["path"] = str(destination)
        artifact_ref["destination_kind"] = "local_dir"
        return _ColumnTablesResolved(
            destination_kind="local_dir",
            artifact_ref=artifact_ref,
            manifest=manifest,
            content=zip_bytes,
            package_filename=package_filename,
            tmp_path=tmp_path,
            destination=destination,
        )

    prefix_or_error = _normalized_project_prefix(params.destination.prefix)
    if isinstance(prefix_or_error, ActionError):
        return prefix_or_error
    project_path = f"{prefix_or_error}/{package_filename}"
    artifact_ref["kind"] = "export_project_file"
    artifact_ref["project_path"] = project_path
    artifact_ref["blob_hash"] = zip_sha256.removeprefix("sha256:")
    artifact_ref["destination_kind"] = "project_file"
    return _ColumnTablesResolved(
        destination_kind="project_file",
        artifact_ref=artifact_ref,
        manifest=manifest,
        content=zip_bytes,
        package_filename=package_filename,
        project_file_prefix=prefix_or_error,
    )


def _normalized_project_prefix(prefix: str) -> str | ActionError:
    stripped = prefix.strip().strip("/")
    parts = [part for part in stripped.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        return _column_tables_error(
            "invalid_export_destination",
            "project_file prefix must be a safe relative path",
            field="params.destination.prefix",
            details={"prefix": prefix},
        )
    return "/".join(parts)


def _build_zip(artifacts: list[_ColumnArtifact], manifest: dict[str, Any]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for artifact in artifacts:
            info = zipfile.ZipInfo(artifact.filename, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, artifact.content)
        manifest_bytes = (
            json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        info = zipfile.ZipInfo("manifest.json", date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_DEFLATED
        zf.writestr(info, manifest_bytes)
    return buffer.getvalue()


def _store_export_column_tables_project_blob(
    project: Project,
    cur: Any,
    *,
    content: bytes,
    filename: str,
    project_path: str,
) -> None:
    digest = hashlib.sha256(content).hexdigest()
    if project.blob_store.put(content) != digest:
        raise ValueError("column-tables blob store returned the wrong digest")
    metadata = {
        "frisket_role": "export_artifact",
        "export_kind": "column_tables",
        "project_path": project_path,
        "filename": filename,
    }
    cur.execute(
        "INSERT OR IGNORE INTO blobs (hash, filename, mime, size, source_url, metadata) "
        "VALUES (?,?,?,?,?,?)",
        (
            digest,
            filename,
            "application/zip",
            len(content),
            None,
            json.dumps(metadata, sort_keys=True),
        ),
    )
    MediaBlobStore(project).merge_metadata(digest, metadata)


def _export_column_tables_receipt(
    *,
    action: _TypedProjectEnvelope,
    action_id: str,
    project_id: str,
    receipt_id: str,
    params_hash: str,
    artifact_ref: dict[str, Any],
    manifest: dict[str, Any],
) -> Receipt:
    summary = {
        "kind": "column_tables_export_summary",
        "artifact_count": manifest["artifact_count"],
        "entry_count": manifest["entry_count"],
        "shape": manifest["shape"],
        "group_by": manifest["group_by"],
        "byte_count": artifact_ref["byte_count"],
    }
    return Receipt(
        receipt_id=receipt_id,
        project_id=project_id,
        action_id=action_id,
        action_kind=action.kind,
        idempotency_key=action.idempotency_key,
        params_hash=params_hash,
        status="completed",
        inputs=[
            ReceiptIO(
                name="source.column",
                ref={
                    "kind": "sheet_column",
                    "sheet_id": manifest["sheet_id"],
                    "column_id": manifest["column_id"],
                    "column_name": manifest["column_name"],
                },
            )
        ],
        outputs=[ReceiptIO(name="column_tables", ref=artifact_ref)],
        evidence=[
            ReceiptEvidence(
                ref={"kind": "column_tables_export_manifest", **manifest},
                retention="pinned",
            ),
            ReceiptEvidence(ref=summary, retention="pinned"),
        ],
        exports=[artifact_ref],
    )


def _perform_export_column_tables_in_txn(
    project: Project,
    cur: Any,
    action: _TypedProjectEnvelope,
    params: Any,
    *,
    project_id: str,
    action_id: str,
    receipt_id: str,
    params_hash: str,
    resolved: _ColumnTablesResolved,
) -> ActionResult:
    result = ActionResult(
        action=ActionIdentity(kind=action.kind, action_id=action_id),
        status="completed",
        project_id=project_id,
        outputs=[
            ActionOutput(kind="export", name="column_tables", ref=resolved.artifact_ref)
        ],
        receipt_id=receipt_id,
    )
    receipt = _export_column_tables_receipt(
        action=action,
        action_id=action_id,
        project_id=project_id,
        receipt_id=receipt_id,
        params_hash=params_hash,
        artifact_ref=resolved.artifact_ref,
        manifest=resolved.manifest,
    )
    if resolved.destination_kind == "project_file":
        _store_export_column_tables_project_blob(
            project,
            cur,
            content=resolved.content,
            filename=resolved.package_filename,
            project_path=resolved.artifact_ref["project_path"],
        )
        ReceiptStore(project).insert_completed(receipt, commit=False)
    else:
        ReceiptStore(project).insert_completed(receipt, commit=False)
        assert resolved.tmp_path is not None and resolved.destination is not None
        resolved.delivery = _deliver_export_artifact(
            resolved.tmp_path, resolved.destination
        )
    return result


def _cleanup_export_column_tables(resolved: _ColumnTablesResolved) -> None:
    if resolved.destination_kind == "local_dir":
        assert resolved.tmp_path is not None and resolved.destination is not None
        _rollback_export_delivery(
            delivery=resolved.delivery,
            tmp_path=resolved.tmp_path,
            destination=resolved.destination,
        )


def _finalize_export_column_tables(resolved: _ColumnTablesResolved) -> None:
    if resolved.destination_kind == "local_dir" and resolved.delivery is not None:
        _finalize_export_delivery(resolved.delivery)


def _export_column_tables_replay_error(
    project: Project, receipt: Receipt
) -> ActionError | None:
    action_kind = receipt.action_kind
    refs = [
        ref
        for ref in receipt.exports
        if isinstance(ref, dict) and ref.get("export_kind") == "column_tables"
    ]
    if not refs:
        return ActionError(
            code="export_artifact_missing",
            message=f"{action_kind} replay receipt does not include an artifact ref",
            action_kind=action_kind,
            details={"receipt_id": receipt.receipt_id},
        )
    ref = refs[0]
    if ref.get("destination_kind") == "project_file":
        blob_hash = ref.get("blob_hash")
        if not isinstance(blob_hash, str) or not blob_hash:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay project-file blob hash is missing",
                action_kind=action_kind,
                details={"receipt_id": receipt.receipt_id},
            )
        if MediaBlobStore(project).blob_row(blob_hash) is None:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay project-file blob row is missing",
                action_kind=action_kind,
                details={"receipt_id": receipt.receipt_id, "blob_hash": blob_hash},
            )
        try:
            content = project.read_blob(blob_hash)
        except BlobIntegrityError:
            return ActionError(
                code="export_artifact_mismatch",
                message=f"{action_kind} replay artifact hash changed",
                action_kind=action_kind,
                details={"receipt_id": receipt.receipt_id, "blob_hash": blob_hash},
            )
        except (BlobNotFoundError, OSError) as exc:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay artifact is missing",
                action_kind=action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "blob_hash": blob_hash,
                    "error": str(exc),
                },
            )
    else:
        path_raw = ref.get("path")
        if not isinstance(path_raw, str) or not path_raw:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay artifact path is missing",
                action_kind=action_kind,
                details={"receipt_id": receipt.receipt_id},
            )
        path = Path(path_raw)
        try:
            content = path.read_bytes()
        except OSError as exc:
            return ActionError(
                code="export_artifact_missing",
                message=f"{action_kind} replay artifact is missing",
                action_kind=action_kind,
                details={
                    "receipt_id": receipt.receipt_id,
                    "path": str(path),
                    "error": str(exc),
                },
            )

    expected_bytes = ref.get("byte_count")
    if isinstance(expected_bytes, int) and expected_bytes != len(content):
        return ActionError(
            code="export_artifact_mismatch",
            message=f"{action_kind} replay artifact byte count changed",
            action_kind=action_kind,
            details={
                "receipt_id": receipt.receipt_id,
                "expected_byte_count": expected_bytes,
                "actual_byte_count": len(content),
            },
        )
    expected_sha256 = ref.get("sha256")
    actual_sha256 = "sha256:" + hashlib.sha256(content).hexdigest()
    if isinstance(expected_sha256, str) and expected_sha256 != actual_sha256:
        return ActionError(
            code="export_artifact_mismatch",
            message=f"{action_kind} replay artifact hash changed",
            action_kind=action_kind,
            details={
                "receipt_id": receipt.receipt_id,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
            },
        )
    return None


class _ColumnTablesResolutionFailed(Exception):
    def __init__(self, error: ActionError):
        super().__init__(error.message)
        self.error = error


class _ColumnTablesExportCapability:
    def __init__(self, project: Project, project_id: str):
        self.project = project
        self.project_id = project_id
        self.resolution: _ColumnTablesResolved | None = None
        self.called = False

    def write_column_tables(
        self,
        *,
        sheet_id: int,
        column_id: int,
        destination: ColumnTablesDestination,
        group_by: str | None = None,
        exclude_columns: list[str] | None = None,
        name_template: str = "{row:03d}_{table:03d}.csv",
        first_row_header: bool = False,
    ) -> ColumnTablesExport:
        if self.called:
            raise TypeError("column-tables exporter must be called exactly once")
        self.called = True
        # Validate this operation's actual arguments, not the action author's
        # possibly differently named or derived Params.
        try:
            arguments = ExportColumnTablesParams(
                sheet_id=sheet_id,
                column_id=column_id,
                destination=(
                    destination.model_dump(mode="python")
                    if isinstance(destination, BaseModel)
                    else destination
                ),
                group_by=group_by,
                exclude_columns=exclude_columns,
                name_template=name_template,
                first_row_header=first_row_header,
            )
        except ValidationError as exc:
            raise _ColumnTablesResolutionFailed(
                _column_tables_error("invalid_params", "export arguments are invalid")
            ) from exc
        resolved = _resolve_export_column_tables(
            self.project, arguments, project_id=self.project_id
        )
        if isinstance(resolved, ActionError):
            raise _ColumnTablesResolutionFailed(resolved)
        self.resolution = resolved
        # The handler may reconstruct or alter its domain result. It does not
        # own the operation facts subsequently recorded in the receipt.
        return ColumnTablesExport.model_validate(
            {**deepcopy(resolved.artifact_ref), "manifest": deepcopy(resolved.manifest)}
        )


def _column_tables_resolver(terminal: _ProjectAction[Any, Any], *, project_id: str):
    def resolve(project: Project, params: Any) -> _ColumnTablesResolved | ActionError:
        capability = _ColumnTablesExportCapability(project, project_id)
        try:
            returned = terminal.handler(params, capability)
            if capability.resolution is None or not isinstance(
                returned, ColumnTablesExport
            ):
                raise TypeError(
                    "column-tables handler must export and return ColumnTablesExport"
                )
            terminal.output_model.model_validate(returned.model_dump(mode="python"))
        except _ColumnTablesResolutionFailed as exc:
            if capability.resolution is not None:
                _cleanup_export_column_tables(capability.resolution)
            return exc.error
        except BaseException:
            if capability.resolution is not None:
                _cleanup_export_column_tables(capability.resolution)
            raise
        return capability.resolution

    return resolve


def _run_typed_export_column_tables(
    project: Project,
    project_id: str,
    bound: BoundTypedActionRequest,
    *,
    deps: ExecutorDeps | None = None,
) -> ActionResult:
    terminal = bound.action.definition.run
    if not isinstance(terminal, _ProjectAction) or terminal.capabilities != (
        ColumnTablesExporter,
    ):
        raise TypeError("column-tables executor requires a ColumnTablesExporter action")
    envelope = _TypedProjectEnvelope(
        kind=bound.action.action_id,
        idempotency_key=bound.request.idempotency_key,
        params=bound.params.model_dump(mode="json"),
    )
    params_hash = typed_request_hash(bound)
    spec = _ActionCoreSpec(
        kind=envelope.kind,
        params_model=terminal.params_model,
        body_kind="plain",
        params_hash_fn=lambda _action: params_hash,
        result_from_existing_fn=_child_sheet_deterministic_result_from_existing(
            replay_validate_fn=_export_column_tables_replay_error
        ),
        plain_resolve_fn=_column_tables_resolver(terminal, project_id=project_id),
        plain_perform_in_txn_fn=_perform_export_column_tables_in_txn,
        plain_exception_error_fn=_export_project_write_failed_error,
        plain_cleanup_resolved_fn=_cleanup_export_column_tables,
        plain_post_commit_fn=_finalize_export_column_tables,
    )
    result = _run_action_core_spec(
        project,
        envelope,
        bound.params,
        spec=spec,
        ctx=ExecutorContext(project_id=project_id, deps=deps or ExecutorDeps()),
    )
    _emit_export_notification(project, result)
    return result
