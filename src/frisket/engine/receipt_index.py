"""Rebuildable receipt selection helpers.

Receipt rows remain the durable audit truth. This module keeps a derived
sidecar with receipt metadata, ref-kind summaries, and export artifact refs so
reports and work logs do not need to parse every receipt body on hot paths.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from frisket.engine.store.receipts import ReceiptStore


RECEIPT_INDEX_VERSION = 1


def receipt_index_path(project: Any) -> Path:
    return Path(project.path) / "debug" / "receipts-index.json"


def _json_body(raw: str | None) -> dict[str, Any]:
    try:
        body = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return body if isinstance(body, dict) else {}


def _ref_kind_set(body: dict[str, Any], section: str) -> list[str]:
    kinds: set[str] = set()
    for item in body.get(section) or []:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if isinstance(ref, dict) and ref.get("kind"):
            kinds.add(str(ref["kind"]))
    return sorted(kinds)


def _op_ids(body: dict[str, Any]) -> list[Any]:
    op_ids = body.get("op_ids")
    return op_ids if isinstance(op_ids, list) else []


def _export_artifacts_from_body(
    body: dict[str, Any],
    *,
    default_export_kind: str,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for ref in body.get("exports") or []:
        if not isinstance(ref, dict) or ref.get("kind") != "export_artifact":
            continue
        artifacts.append(
            {
                "kind": ref.get("export_kind") or default_export_kind,
                "format": ref.get("format")
                or ("csv" if default_export_kind == "sheet_csv" else "markdown"),
                "path": ref.get("path"),
                "byte_count": ref.get("byte_count"),
                "sha256": ref.get("sha256"),
            }
        )
    for item in body.get("outputs") or []:
        if not isinstance(item, dict):
            continue
        ref = item.get("ref")
        if not isinstance(ref, dict) or ref.get("kind") != "export_artifact":
            continue
        artifacts.append(
            {
                "kind": ref.get("export_kind") or default_export_kind,
                "format": ref.get("format")
                or ("csv" if default_export_kind == "sheet_csv" else "markdown"),
                "path": ref.get("path"),
                "byte_count": ref.get("byte_count"),
                "sha256": ref.get("sha256"),
            }
        )
    return artifacts


def _receipt_metadata(row: Any) -> dict[str, Any]:
    return {
        "rowid": int(row["rowid"]),
        "id": row["id"],
        "run_id": row["run_id"],
        "action_kind": row["action_kind"],
        "idempotency_key": row["idempotency_key"],
        "status": row["status"],
        "created_at": row["created_at"],
        "body_length": int(row["body_length"] or 0),
    }


def _receipt_watermark(project: Any) -> dict[str, Any]:
    # Receipts are updated in place while queued actions move from queued to
    # terminal states. Until the receipt table exposes a durable revision or
    # updated_at column, keep this validation exact over receipt metadata. This
    # avoids parsing receipt bodies on hot paths while preventing stale indexes
    # after status/run/body-length changes.
    rows = ReceiptStore(project).watermark_rows()
    return {
        "rows": [_receipt_metadata(row) for row in rows],
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    tmp_path.replace(path)


def _receipt_projection(row: Any, body: dict[str, Any]) -> dict[str, Any]:
    receipt_id = str(row["id"])
    return {
        "receipt_id": receipt_id,
        "available": True,
        "action_kind": row["action_kind"],
        "status": row["status"],
        "evidence_refs": len(body.get("evidence") or []),
        "input_refs": len(body.get("inputs") or []),
        "output_refs": len(body.get("outputs") or []),
        "evidence": _ref_kind_set(body, "evidence"),
        "inputs": _ref_kind_set(body, "inputs"),
        "outputs": _ref_kind_set(body, "outputs"),
        "op_ids": _op_ids(body),
        "export_artifacts": _export_artifacts_from_body(
            body,
            default_export_kind="work_log",
        ),
    }


def _build_receipt_index(project: Any) -> dict[str, Any]:
    rows = ReceiptStore(project).index_rows()
    metadata = [_receipt_metadata(row) for row in rows]
    projections = []
    for row in rows:
        body = _json_body(row["body"])
        projections.append(_receipt_projection(row, body))
    index = {
        "schema_version": "frisket.receipt_index.v1",
        "version": RECEIPT_INDEX_VERSION,
        "watermark": {"rows": metadata},
        "receipts": projections,
    }
    _atomic_write_json(receipt_index_path(project), index)
    return index


def build_receipt_index(project: Any) -> dict[str, Any]:
    return _build_receipt_index(project)


def _load_receipt_index(project: Any) -> dict[str, Any]:
    path = receipt_index_path(project)
    current = _receipt_watermark(project)
    if path.exists():
        try:
            with path.open(encoding="utf-8") as f:
                index = json.load(f)
        except (OSError, json.JSONDecodeError):
            index = None
        if (
            isinstance(index, dict)
            and index.get("version") == RECEIPT_INDEX_VERSION
            and index.get("watermark") == current
            and isinstance(index.get("receipts"), list)
        ):
            return index
    return _build_receipt_index(project)


def receipt_metadata_rows(
    project: Any,
    *,
    action_kind: str | None = None,
    receipt_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    wanted_ids = {str(receipt_id) for receipt_id in receipt_ids or []}
    rows = list(_load_receipt_index(project).get("watermark", {}).get("rows") or [])
    out = []
    for row in rows:
        if wanted_ids and str(row.get("id")) not in wanted_ids:
            continue
        if action_kind is not None and row.get("action_kind") != action_kind:
            continue
        out.append(dict(row))
    return out


def receipt_artifacts(
    project: Any,
    *,
    receipt_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    wanted_ids = {str(receipt_id) for receipt_id in receipt_ids or []}
    artifacts = []
    for item in _load_receipt_index(project).get("receipts") or []:
        if not isinstance(item, dict):
            continue
        if wanted_ids and str(item.get("receipt_id")) not in wanted_ids:
            continue
        artifacts.append(
            {
                "receipt_id": str(item.get("receipt_id")),
                "available": bool(item.get("available", True)),
                "action_kind": item.get("action_kind"),
                "status": item.get("status"),
                "evidence_refs": int(item.get("evidence_refs") or 0),
            }
        )
    return artifacts


def receipt_evidence_counts(project: Any) -> dict[str, int]:
    return {
        artifact["receipt_id"]: int(artifact.get("evidence_refs") or 0)
        for artifact in receipt_artifacts(project)
    }


def receipt_bodies_by_id(
    project: Any,
    receipt_ids: Iterable[str],
) -> dict[str, dict[str, Any]]:
    ids = sorted({str(receipt_id) for receipt_id in receipt_ids})
    if not ids:
        return {}
    bodies = ReceiptStore(project).bodies_by_id(ids)
    return {receipt_id: _json_body(body) for receipt_id, body in bodies.items()}


def export_artifacts(
    project: Any,
    *,
    default_export_kind: str,
    receipt_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    wanted_ids = {str(receipt_id) for receipt_id in receipt_ids or []}
    artifacts: list[dict[str, Any]] = []
    for item in _load_receipt_index(project).get("receipts") or []:
        if not isinstance(item, dict):
            continue
        receipt_id = str(item.get("receipt_id"))
        if wanted_ids and receipt_id not in wanted_ids:
            continue
        for artifact in item.get("export_artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            artifacts.append(
                {
                    "receipt_id": receipt_id,
                    "kind": artifact.get("kind") or default_export_kind,
                    "format": artifact.get("format")
                    or ("csv" if default_export_kind == "sheet_csv" else "markdown"),
                    "path": artifact.get("path"),
                    "byte_count": artifact.get("byte_count"),
                    "sha256": artifact.get("sha256"),
                }
            )
    return artifacts


def work_log_receipt_summaries(project: Any) -> list[dict[str, Any]]:
    summaries = []
    by_id = {str(row["id"]): row for row in receipt_metadata_rows(project)}
    for item in _load_receipt_index(project).get("receipts") or []:
        if not isinstance(item, dict):
            continue
        receipt_id = str(item.get("receipt_id"))
        metadata = by_id.get(receipt_id, {})
        summaries.append(
            {
                "id": receipt_id,
                "action_kind": item.get("action_kind"),
                "status": item.get("status"),
                "op_ids": item.get("op_ids") or [],
                "outputs": item.get("outputs") or [],
                "evidence": item.get("evidence") or [],
                "created_at": metadata.get("created_at"),
            }
        )
    summaries.sort(key=lambda item: (item.get("created_at") or "", item["id"]))
    return summaries
