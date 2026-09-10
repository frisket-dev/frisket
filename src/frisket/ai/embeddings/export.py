"""Embedding index export artifact writers.

Writes an index's ready source originals + raw vectors as JSONL, Parquet, or Arrow
IPC. pyarrow backs Parquet/Arrow; JSONL is plain. Raw vectors are NEVER written to
CSV or the default project bundle — this explicit export is the only path.

Each writer goes to a tmp file in the destination dir (so the executor can move it
into place inside the receipt transaction) and reports byte_count + sha256 of the
exact bytes written, for replay validation.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMAT_SUFFIX = {"jsonl": "jsonl", "parquet": "parquet", "arrow": "arrow"}


@dataclass(frozen=True)
class ExportArtifact:
    format: str
    path: str  # final destination path
    tmp_path: str  # written here first; the caller moves it into place
    byte_count: int
    sha256: str
    row_count: int


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str | None:
    """sha256 of an on-disk artifact, or None when it no longer exists — the seam
    a replay uses to verify a prior export's artifacts still match."""
    p = Path(path)
    try:
        return _sha256(p.read_bytes()) if p.is_file() else None
    except OSError:
        return None


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return (
        "".join(
            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n" for row in rows
        )
    ).encode("utf-8")


def _arrow_table(rows: list[dict[str, Any]], include_vectors: bool):
    import pyarrow as pa

    columns = {
        "source_key": pa.array([r["source_key"] for r in rows], pa.string()),
        "row_id": pa.array([r.get("row_id") for r in rows], pa.int64()),
        "source_hash": pa.array([r["source_hash"] for r in rows], pa.string()),
        "values_json": pa.array(
            [json.dumps(r["values"], sort_keys=True, ensure_ascii=False) for r in rows],
            pa.string(),
        ),
    }
    if include_vectors:
        columns["vector"] = pa.array(
            [r.get("vector") for r in rows], pa.list_(pa.float32())
        )
    return pa.table(columns)


def write_export_artifacts(
    rows: list[dict[str, Any]],
    *,
    dest_dir: Path,
    base_name: str,
    formats: list[str],
    include_vectors: bool,
) -> list[ExportArtifact]:
    """Write one tmp artifact per format into ``dest_dir``. Returns the artifacts
    (with final path + tmp path + sha256); the caller commits the move."""
    dest = Path(dest_dir)
    out: list[ExportArtifact] = []
    table = None  # built lazily, shared by parquet + arrow
    staged: list[Path] = []
    try:
        for fmt in formats:
            final = dest / f"{base_name}.{_FORMAT_SUFFIX[fmt]}"
            tmp = dest / f".{base_name}.{fmt}.{uuid.uuid4().hex}.tmp"
            # Own the current path before a writer can partially create it.
            staged.append(tmp)
            if fmt == "jsonl":
                data = _jsonl_bytes(rows)
                tmp.write_bytes(data)
            else:
                if table is None:
                    table = _arrow_table(rows, include_vectors)
                if fmt == "parquet":
                    import pyarrow.parquet as pq

                    pq.write_table(table, tmp)
                else:  # arrow IPC (Feather v2)
                    import pyarrow.feather as feather

                    feather.write_feather(table, tmp)
                data = tmp.read_bytes()
            out.append(
                ExportArtifact(
                    format=fmt,
                    path=str(final),
                    tmp_path=str(tmp),
                    byte_count=len(data),
                    sha256=_sha256(data),
                    row_count=len(rows),
                )
            )
    except BaseException:
        for tmp in staged:
            tmp.unlink(missing_ok=True)
        raise
    return out
