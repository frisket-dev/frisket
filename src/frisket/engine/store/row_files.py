"""Closed returned-file checkpoint payload and blob metadata registration."""

from typing import Any, Literal
import json
import copy
from pydantic import BaseModel, ConfigDict, Field


class RowFileBlob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    blob_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, strict=True)
    filename: str
    mime: str
    source_url: str | None
    metadata: dict[str, Any]
    role: str


class RowFileOccurrence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1] = 1
    row_id: int = Field(gt=0, strict=True)
    output_key: str
    item_path: list[str | int]
    primary: RowFileBlob
    supplemental: list[RowFileBlob]
    facts: dict[str, Any]


def row_file_descriptors(results):
    for payload in results.values():
        for item in payload.get("row_files", []):
            yield RowFileOccurrence.model_validate(item)


def register_returned_row_files(project, results):
    """Called inside the existing fenced checkpoint/result transaction."""
    from frisket.engine.store.media_blobs import MediaBlobStore

    for descriptor in row_file_descriptors(results):
        for blob in (descriptor.primary, *descriptor.supplemental):
            project.db.execute(
                "INSERT OR IGNORE INTO blobs(hash,filename,mime,size,source_url,metadata) VALUES(?,?,?,?,?,?)",
                (
                    blob.blob_hash,
                    blob.filename,
                    blob.mime,
                    blob.size,
                    blob.source_url,
                    json.dumps(blob.metadata),
                ),
            )
            if blob.metadata:
                MediaBlobStore(project).merge_metadata(blob.blob_hash, blob.metadata)


def record_row_file_calls(project, batch, *, run_id, writer_attempt_id, claim_token):
    """Observed calls survive cancellation even when their output is withheld."""
    from frisket.engine.store.receipts import ReceiptStore

    for result in batch:
        for call in result.get("row_file_calls", []):
            if call.get("kind") == "document_convert_read":
                # Conversion owns this read's evidence; it is not a file acquisition.
                continue
            ReceiptStore(project)._record_writer_evidence(
                {
                    **copy.deepcopy(call),
                    "observation_kind": call.get("kind"),
                    "kind": "row_file_call",
                    "row_id": result["row_id"],
                },
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
            )
