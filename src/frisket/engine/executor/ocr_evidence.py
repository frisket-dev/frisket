"""Exact-output OCR grounding from checkpointed source and raster facts."""

from __future__ import annotations

import json

from frisket.engine.store.evidence import (
    mark_evidence_stale_for_cell_refs,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.grounding import normalize_bbox
from frisket.engine.store.media_blobs import (
    MediaBlobStore,
    owned_media_metadata_document,
)
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.receipts import ReceiptStore


def _register_page_images(project, page_images):
    """The reader promotes CAS bytes; only the accepted writer registers them."""
    blobs = MediaBlobStore(project)
    for image in page_images.values():
        if image.get("source_blob"):
            continue
        metadata = owned_media_metadata_document(
            probe={
                "kind": "image",
                "width": image.get("width"),
                "height": image.get("height"),
            },
            owner={"page_image": True},
        )
        project.db.execute(
            "INSERT OR IGNORE INTO blobs(hash,filename,mime,size,source_url,metadata) VALUES(?,?,?,?,?,?)",
            (
                image["blob_hash"],
                image.get("filename"),
                image["mime"],
                image["size"],
                None,
                json.dumps(metadata),
            ),
        )
        blobs.merge_metadata(image["blob_hash"], metadata)


def _page_spans(entry, page_images, engine):
    page = entry["page"]
    blocks = entry.get("blocks") or []
    image = page_images.get(str(page)) or {}
    # Stored display PNGs may be capped; engine coordinates use the original raster.
    width = image.get("source_width") or image.get("width")
    height = image.get("source_height") or image.get("height")
    text = " ".join(
        str(block.get("text") or "") for block in blocks if isinstance(block, dict)
    ).strip()
    yield {
        "span_kind": "page_range",
        "page_start": page,
        "page_end": page,
        "snippet": text or f"Page {page}",
        "metadata": {"engine": engine},
    }
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        bbox = (
            normalize_bbox(block.get("bbox"), frame="page", width=width, height=height)
            if width and height
            else None
        )
        if bbox is None:
            if text:
                yield {
                    "span_kind": "page_range",
                    "page_start": page,
                    "page_end": page,
                    "quote": text,
                    "snippet": text,
                    "metadata": {"raw": block},
                }
        else:
            yield {
                "span_kind": "region",
                "page_start": page,
                "page_end": page,
                "bbox": [bbox],
                "quote": text or None,
                "snippet": text or None,
                "selector": {
                    "polygon": block.get("bbox"),
                    "engine": engine,
                    "score": block.get("score"),
                },
                "metadata": {"raw": block},
            }


def write_ocr_evidence(
    project,
    spec,
    *,
    batch,
    run_id,
    op_id,
    output_columns,
    ocr_columns: set[int],
    writer_attempt_id,
    claim_token,
    **kwargs,
):
    """Publish precise links only for declared OcrText equal to the actual return.

    Called inside the existing result savepoint. No rasterization, live source
    reads, or dependency on a sibling blocks column occurs at publication/replay.
    """
    reads = {
        int(item["row_id"]): call
        for item in batch
        for call in item.get("row_file_calls") or []
        if isinstance(call, dict) and call.get("kind") == "ocr_read"
    }
    if not reads or not ocr_columns:
        return
    OutputColumnClaimStore.require_current_writer(
        project.db,
        run_id=run_id,
        writer_attempt_id=writer_attempt_id,
        claim_token=claim_token,
    )
    receipt_id = project.db.execute(
        "SELECT DISTINCT receipt_id FROM output_column_claims "
        "WHERE run_id=? AND claim_token=? AND status='active'",
        (run_id, claim_token),
    ).fetchone()[0]
    store = ReceiptStore(project)
    for item in batch:
        row_id, column_id = int(item["row_id"]), int(item["column_id"])
        read, value = reads.get(row_id), item.get("value")
        if (
            column_id not in ocr_columns
            or item.get("error") is not None
            or read is None
            or not isinstance(value, str)
            or value != read["text"]
        ):
            continue
        if (
            project.db.execute(
                "SELECT 1 FROM evidence_links WHERE run_id=? AND row_id=? AND column_id=? "
                "AND link_role='media_ocr_grounding' LIMIT 1",
                (run_id, row_id, column_id),
            ).fetchone()
            is not None
        ):
            continue
        pages = [
            entry
            for entry in read["blocks"]
            if isinstance(entry, dict)
            and type(entry.get("page")) is int
            and entry["page"] > 0
        ]
        if not pages:
            continue
        source, engine, page_images = (
            read["source"],
            read["engine"],
            read["page_images"],
        )
        if not source.get("blob_hash"):
            continue
        _register_page_images(project, page_images)
        artifact = record_source_artifact(
            project,
            artifact_kind="file",
            media_type=source.get("mime") or "application/octet-stream",
            blob_hash=source["blob_hash"],
            filename=source.get("filename"),
            page_count=len({entry["page"] for entry in pages}),
            source_sheet_id=source["sheet_id"],
            source_row_id=row_id,
            source_column_id=source["column_id"],
            metadata={
                "engine": engine,
                "dpi": read["options"].get("dpi", 200),
                "page_images": page_images,
            },
        )
        spans = []
        for page in pages:
            for descriptor in _page_spans(page, page_images, engine):
                span = record_source_span(
                    project, artifact_id=artifact["id"], **descriptor
                )
                spans.append({"span_id": span["id"], "rank": len(spans)})
        mark_evidence_stale_for_cell_refs(
            project,
            [{"row_id": row_id, "column_id": column_id, "match_all_active": True}],
            reason="source_reprocessed",
        )
        link = record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref={
                "kind": "run_result",
                "run_id": run_id,
                "op_id": op_id,
                "row_id": row_id,
                "column_id": column_id,
            },
            spans=spans,
            sheet_id=source["sheet_id"],
            row_id=row_id,
            column_id=column_id,
            run_id=run_id,
            op_id=op_id,
            receipt_id=receipt_id,
            link_role="media_ocr_grounding",
            producer={"action_kind": spec["action_kind"], "engine": engine},
        )
        store._record_writer_evidence(
            {
                "kind": "media_ocr_grounding_evidence_link",
                "sheet_id": source["sheet_id"],
                "row_id": row_id,
                "column_id": column_id,
                "artifact_id": artifact["id"],
                "artifact_stable_id": artifact["stable_id"],
                "evidence_link_id": link["id"],
                "stable_id": link["stable_id"],
            },
            run_id=run_id,
            writer_attempt_id=writer_attempt_id,
            claim_token=claim_token,
        )
