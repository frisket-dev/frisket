"""Frozen split plans inside the existing owned import intake directory."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .import_intake import import_intake_dir, read_import_header
from .import_sessions import ImportSessionStore
from .receipts import ReceiptStore


PDF_SPLIT_PLAN_FILE = "pdf-split.json"


def split_plan_hash(plan: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            plan, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def write_pdf_split_plan(directory: Path, plan: Mapping[str, Any]) -> str:
    payload = dict(plan)
    digest = split_plan_hash(payload)
    temporary = directory / ".pdf-split.tmp"
    with temporary.open("w") as sink:
        json.dump(
            {"plan_hash": digest, "plan": payload},
            sink,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        sink.flush()
        os.fsync(sink.fileno())
    os.replace(temporary, directory / PDF_SPLIT_PLAN_FILE)
    return digest


def read_pdf_split_plan(
    directory: Path, *, expected_hash: str | None = None
) -> dict[str, Any]:
    payload = json.loads((directory / PDF_SPLIT_PLAN_FILE).read_text())
    plan = payload["plan"]
    observed = split_plan_hash(plan)
    if observed != payload["plan_hash"] or (
        expected_hash is not None and observed != expected_hash
    ):
        raise ValueError("frozen PDF split plan changed")
    return plan


def pdf_split_status(project: Any, import_ref: str) -> dict[str, Any]:
    """Bounded durable projection shared by import status and packet review."""
    directory = import_intake_dir(project.path, import_ref)
    header = read_import_header(directory)
    plan = read_pdf_split_plan(directory)
    if header.storage_identity != project.storage_identity:
        raise ValueError("PDF split belongs to another project storage identity")
    session = ImportSessionStore(project).get(import_ref)
    receipt = ReceiptStore(project).find_by_id(str(header.envelope["receipt_id"]))
    counts = {
        str(row[0]): int(row[1])
        for row in project.db.execute(
            "SELECT state,COUNT(*) FROM import_session_pdf_sources WHERE session_id=? GROUP BY state",
            (import_ref,),
        )
    }
    pending = counts.get("pending", len(plan["sources"]) if session is None else 0)
    error_rows = int(
        project.db.execute(
            "SELECT COUNT(*) FROM import_session_pdf_sources WHERE session_id=? AND state='published' AND error IS NOT NULL",
            (import_ref,),
        ).fetchone()[0]
    )
    error_groups = []
    for error in project.db.execute(
        "SELECT error,COUNT(*) FROM import_session_pdf_sources WHERE session_id=? AND error IS NOT NULL GROUP BY error ORDER BY COUNT(*) DESC,error LIMIT 10",
        (import_ref,),
    ):
        examples = []
        for example in project.db.execute(
            "SELECT ordinal FROM import_session_pdf_sources WHERE session_id=? AND error=? ORDER BY ordinal LIMIT 3",
            (import_ref, str(error[0])),
        ):
            ordinal = int(example[0])
            examples.append(
                {
                    "source_ordinal": ordinal + 1,
                    "column_name": "",
                    "original": str(plan["sources"][ordinal]["filename"])[:512],
                }
            )
        error_groups.append(
            {
                "code": "pdf_split_source_failed",
                "message": str(error[0]),
                "count": int(error[1]),
                "examples": examples,
            }
        )
    return {
        "import_ref": import_ref,
        "receipt_id": str(header.envelope["receipt_id"]),
        "sheet_id": None if session is None else session.sheet_id,
        "sheet_name": plan["sheet_name"],
        "status": "queued"
        if session is None
        else (
            "running"
            if session.state in ("active", "paused", "cancelled")
            else receipt.status
            if receipt
            else "failed"
        ),
        "document_count": 0 if session is None else session.committed_rows,
        "source_count": len(plan["sources"]),
        "selected_total": len(plan["sources"]) + int(plan.get("skipped_duplicates", 0)),
        "skipped_duplicates": int(plan.get("skipped_duplicates", 0)),
        "pdf_source_count": sum(source["kind"] == "pdf" for source in plan["sources"]),
        "source_attempted": counts.get("published", 0) + counts.get("failed", 0),
        "source_succeeded": counts.get("published", 0) - error_rows,
        "source_failed": counts.get("failed", 0) + error_rows,
        "source_skipped": pending
        if session is not None and session.state in ("kept", "completed")
        else 0,
        "expected_rows": sum(len(source["children"]) for source in plan["sources"]),
        "committed_bytes": 0 if session is None else session.committed_bytes,
        "admitted_bytes": sum(source["size"] for source in plan["sources"]),
        "cancel_requested": header.cancel_requested,
        "source_error_groups": error_groups,
    }


__all__ = [
    "PDF_SPLIT_PLAN_FILE",
    "split_plan_hash",
    "write_pdf_split_plan",
    "read_pdf_split_plan",
    "pdf_split_status",
]
