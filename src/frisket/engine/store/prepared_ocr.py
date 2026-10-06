"""Publish OCR page bodies once inside the existing result/checkpoint transaction."""

from __future__ import annotations

from typing import Any

from .prepared_content import PreparedContentStore, PreparedPageDraft
from .value_codec import repair_unicode_text


def prepare_ocr_results(project, run_id: int, results: list[dict[str, Any]]) -> None:
    """Replace host-owned OCR facts and unchanged OcrText outputs with exact refs.

    The typed executor marks eligible output fields. Arbitrary strings (including
    rewritten OCR text) remain ordinary values. The caller owns commit/rollback.
    """
    from .evidence import record_source_artifact

    calls = [
        call
        for result in results
        for call in result.get("row_file_calls", ())
        if call.get("kind") == "ocr_read"
    ]
    if not calls:
        return
    if not project.db.in_transaction:
        raise RuntimeError("OCR preparation requires the publication transaction")
    store = PreparedContentStore(project)
    refs: dict[str, int] = {}
    for call in calls:
        call_id = call["call_id"]
        if call_id in refs:
            ref_id = refs[call_id]
        elif type(call.get("prepared_ref_id")) is int:
            ref_id = call["prepared_ref_id"]
        else:
            pages = call.get("pages")
            if not isinstance(pages, list) or not pages:
                # Older/incomplete observations have no trustworthy page bodies.
                continue
            source = call["source"]
            drafts = []
            for index, page in enumerate(pages, 1):
                number = page.get("page_number", source.get("page") or index)
                image = call.get("page_images", {}).get(str(number), {})
                drafts.append(
                    PreparedPageDraft(
                        page_number=number,
                        text=page["text"],
                        positions={
                            "engine": call["engine"],
                            "width": image.get("source_width"),
                            "height": image.get("source_height"),
                            "blocks": page.get("blocks", []),
                        },
                    )
                )
            page_count = source.get("page_count") or max(
                page.page_number for page in drafts
            )
            artifact = project.db.execute(
                "SELECT id FROM source_artifacts WHERE blob_hash=? "
                "AND source_sheet_id=? AND source_row_id=? AND source_column_id=? "
                "AND page_count=? ORDER BY id DESC LIMIT 1",
                (
                    source["blob_hash"],
                    source["sheet_id"],
                    source["row_id"],
                    source["column_id"],
                    page_count,
                ),
            ).fetchone()
            artifact_id = (
                int(artifact[0])
                if artifact
                else int(
                    record_source_artifact(
                        project,
                        artifact_kind="file",
                        media_type=source["mime"],
                        blob_hash=source["blob_hash"],
                        filename=source.get("filename"),
                        page_count=page_count,
                        source_sheet_id=source["sheet_id"],
                        source_row_id=source["row_id"],
                        source_column_id=source["column_id"],
                    )["id"]
                )
            )
            op = project.db.execute(
                "SELECT op_id FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if op is None:
                raise ValueError("OCR preparation requires an existing run")
            ref_id = store.stage_reference(
                source_artifact_id=artifact_id,
                producing_op_id=int(op[0]),
                pages=drafts,
                page_number=source.get("page"),
            ).ref_id
        refs[call_id] = ref_id
        call["prepared_ref_id"] = ref_id
        for field in ("text", "blocks", "pages"):
            call.pop(field, None)
    resolved = store.resolve_many(list(refs.values()))
    for result in results:
        call_id = result.pop("prepared_ocr_call_id", None)
        ref_id = refs.get(call_id)
        if ref_id is not None and result.get("error") is None:
            value = result.get("value")
            if (
                not isinstance(value, str)
                or repair_unicode_text(value) != resolved[ref_id].text
            ):
                raise ValueError("OCR output does not match its prepared text")
            result["prepared_ref_id"] = ref_id
            result["value"] = None


def hydrate_prepared_results(project, results: list[dict[str, Any]]) -> None:
    """Restore transient strings after loading a compact durable response."""
    refs = [item["prepared_ref_id"] for item in results if "prepared_ref_id" in item]
    if not refs:
        return
    values = PreparedContentStore(project).resolve_many(refs)
    for item in results:
        if "prepared_ref_id" in item:
            item["value"] = values[item["prepared_ref_id"]].text
