"""Streaming named-index export artifact writers."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMAT_SUFFIX = {"jsonl": "jsonl", "parquet": "parquet", "arrow": "arrow"}


@dataclass(frozen=True)
class ExportArtifact:
    format: str
    path: str
    tmp_path: str
    byte_count: int
    sha256: str
    row_count: int


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_file(path: str) -> str | None:
    """Return a bounded-memory digest of an artifact, or None when absent."""

    source = Path(path)
    if not source.is_file():
        return None
    digest = hashlib.sha256()
    try:
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


def _arrow_schema(include_vectors: bool):
    import pyarrow as pa

    fields = [
        pa.field("source_key", pa.string()),
        pa.field("row_id", pa.int64()),
        pa.field("source_hash", pa.string()),
        pa.field("values_json", pa.string()),
    ]
    if include_vectors:
        fields.append(pa.field("vector", pa.list_(pa.float32())))
    return pa.schema(fields)


def _arrow_batch(rows: Sequence[dict[str, Any]], schema: Any):
    import pyarrow as pa

    values: dict[str, Any] = {
        "source_key": [row["source_key"] for row in rows],
        "row_id": [row.get("row_id") for row in rows],
        "source_hash": [row["source_hash"] for row in rows],
        "values_json": [
            json.dumps(row["values"], sort_keys=True, ensure_ascii=False)
            for row in rows
        ],
    }
    if "vector" in schema.names:
        values["vector"] = [row.get("vector") for row in rows]
    return pa.RecordBatch.from_pylist(
        [
            {name: values[name][offset] for name in schema.names}
            for offset in range(len(rows))
        ],
        schema=schema,
    )


def write_export_artifacts(
    row_pages: Iterable[Sequence[dict[str, Any]]],
    *,
    dest_dir: Path,
    base_name: str,
    formats: list[str],
    include_vectors: bool,
) -> list[ExportArtifact]:
    """Write every row page to all requested formats without retaining prior pages."""

    dest = Path(dest_dir)
    schema = _arrow_schema(include_vectors) if set(formats) - {"jsonl"} else None
    staged: dict[str, Path] = {}
    finals: dict[str, Path] = {}
    jsonl_handle = None
    parquet_writer = None
    arrow_sink = None
    arrow_writer = None
    row_count = 0
    try:
        for fmt in formats:
            finals[fmt] = dest / f"{base_name}.{_FORMAT_SUFFIX[fmt]}"
            staged[fmt] = dest / f".{base_name}.{fmt}.{uuid.uuid4().hex}.tmp"
        if "jsonl" in staged:
            jsonl_handle = staged["jsonl"].open("wb")
        if "parquet" in staged:
            import pyarrow.parquet as pq

            parquet_writer = pq.ParquetWriter(staged["parquet"], schema)
        if "arrow" in staged:
            import pyarrow as pa

            arrow_sink = pa.OSFile(str(staged["arrow"]), "wb")
            arrow_writer = pa.ipc.new_file(arrow_sink, schema)

        for page in row_pages:
            if not page:
                continue
            row_count += len(page)
            if jsonl_handle is not None:
                for row in page:
                    jsonl_handle.write(
                        (
                            json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                        ).encode("utf-8")
                    )
            if parquet_writer is not None or arrow_writer is not None:
                batch = _arrow_batch(page, schema)
                if parquet_writer is not None:
                    parquet_writer.write_batch(batch)
                if arrow_writer is not None:
                    arrow_writer.write_batch(batch)
        if arrow_writer is not None:
            arrow_writer.close()
            arrow_writer = None
        if arrow_sink is not None:
            arrow_sink.close()
            arrow_sink = None
        if parquet_writer is not None:
            parquet_writer.close()
            parquet_writer = None
        if jsonl_handle is not None:
            jsonl_handle.close()
            jsonl_handle = None
    except BaseException:
        for writer in (arrow_writer, parquet_writer, jsonl_handle, arrow_sink):
            if writer is not None:
                try:
                    writer.close()
                except Exception:
                    pass
        for path in staged.values():
            path.unlink(missing_ok=True)
        raise

    return [
        ExportArtifact(
            format=fmt,
            path=str(finals[fmt]),
            tmp_path=str(staged[fmt]),
            byte_count=staged[fmt].stat().st_size,
            sha256=sha256_file(str(staged[fmt])) or _sha256(b""),
            row_count=row_count,
        )
        for fmt in formats
    ]
