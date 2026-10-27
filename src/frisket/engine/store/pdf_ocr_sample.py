"""Publish an explicit original-page OCR sample under its provider receipt."""

from __future__ import annotations

import json
from typing import Any, Mapping

from frisket.engine.executor.document_extraction_read import document_cell
from frisket.engine.runner.publication import PUBLISH_VALUE
from frisket.engine.runner.result_generations import _compatibility_key
from frisket.engine.store.document_text import (
    DocumentTextUnavailable,
    document_text_role,
    ensure_document_text_role,
    resolve_document_text_selection,
)
from frisket.engine.store.evidence import (
    record_evidence_link,
    record_source_artifact,
    record_source_span,
)
from frisket.engine.store.grounding import normalize_bbox
from frisket.engine.store.output_claims import OutputColumnClaimStore
from frisket.engine.store.prepared_content import (
    PreparedContentStore,
    PreparedPageDraft,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.result_generations import ResultGenerationStore
from frisket.engine.store.runs import RunResultStore


def _text_identity(
    project: Any, row_id: int, source_column_id: int
) -> tuple[int | None, int | None]:
    role = document_text_role(project, source_column_id=source_column_id)
    if role is None:
        return None, None
    row = project.db.execute(
        "SELECT retrieval_revision FROM current_cells WHERE row_id=? AND column_id=?",
        (row_id, role.text_column_id),
    ).fetchone()
    return role.text_column_id, int(row[0]) if row is not None else None


def capture_pdf_ocr_sample(
    project: Any,
    *,
    sheet_id: int,
    row_id: int,
    source_column_id: int,
    blob_hash: str,
    filename: str,
    page_count: int,
    page_number: int,
    expected_source_value: Any,
    ocr_action_params: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze source and text identity without creating history before provider work."""
    cell = document_cell(
        project, sheet_id=sheet_id, column_id=source_column_id, row_id=row_id
    )
    current = project.get_values(sheet_id, source_column_id, row_ids=[row_id]).get(
        row_id
    )
    if (
        current != expected_source_value
        or cell.blob_id != blob_hash
        or not 1 <= page_number <= page_count
        or (cell.page is not None and cell.page != page_number)
    ):
        raise ValueError("source file changed; refresh setup")
    column, revision = _text_identity(project, row_id, source_column_id)
    return {
        "sheet_id": sheet_id,
        "row_id": row_id,
        "source_column_id": source_column_id,
        "blob_hash": blob_hash,
        "filename": filename,
        "page_count": page_count,
        "page_number": page_number,
        "expected_source_value": expected_source_value,
        "text_column_id": column,
        "text_revision": revision,
        "ocr_action_params": dict(ocr_action_params),
    }


def publish_pdf_ocr_sample(
    project: Any,
    capture: Mapping[str, Any],
    *,
    page: Mapping[str, Any],
    receipt_id: str,
) -> dict[str, int]:
    """Atomically publish already-accounted output as a normal OCR run and Undo step."""
    sheet, row, column = (
        int(capture["sheet_id"]),
        int(capture["row_id"]),
        int(capture["source_column_id"]),
    )
    with project.transaction_scope():
        fresh = capture_pdf_ocr_sample(
            project,
            sheet_id=sheet,
            row_id=row,
            source_column_id=column,
            blob_hash=str(capture["blob_hash"]),
            filename=str(capture["filename"]),
            page_count=int(capture["page_count"]),
            page_number=int(capture["page_number"]),
            expected_source_value=capture["expected_source_value"],
            ocr_action_params=capture["ocr_action_params"],
        )
        if (fresh["text_column_id"], fresh["text_revision"]) != (
            capture["text_column_id"],
            capture["text_revision"],
        ):
            raise ValueError("document text changed while OCR was running")
        if page.get("page") != capture["page_number"]:
            raise ValueError("OCR returned another original page")
        receipt_store = ReceiptStore(project)
        receipt = receipt_store.parsed_by_id(receipt_id)
        if (
            receipt is None
            or receipt.status != "running"
            or receipt.action_kind != "media.ocr"
            or receipt.run_id is not None
            or not receipt.params_hash
        ):
            raise ValueError("OCR receipt is not available for publication")
        role = document_text_role(project, source_column_id=column)
        try:
            selection = (
                resolve_document_text_selection(project, (sheet, row, column))
                if role
                else None
            )
        except DocumentTextUnavailable:
            selection = None
        manual = selection is not None and selection.prepared_ref_id is None
        op_id = project.append_op(
            "media.ocr",
            {"source_column_id": column, "page": int(capture["page_number"])},
            label="OCR sample",
            commit=False,
        )
        created = []
        if role is None or manual:
            names = {
                str(item["name"])
                for item in project.columns(sheet, include_hidden=True)
            }
            name = "OCR sample" if manual else "text"
            base = name
            index = 2
            while name in names:
                name = f"{base} {index}"
                index += 1
            output_column = project.add_column(
                sheet, name, type="text", ai_generated=True, hidden=manual, commit=False
            )
            created.append(output_column)
            if not manual:
                ensure_document_text_role(
                    project,
                    source_column_id=column,
                    text_column_id=output_column,
                    commit=False,
                )
        else:
            output_column = role.text_column_id
            name = str(project.get_column(output_column)["name"])
        if created:
            project.set_undo_info(op_id, {"created_columns": created}, commit=False)
        if selection is not None:
            artifact_id = selection.source_artifact_id
        else:
            artifact_id = int(
                record_source_artifact(
                    project,
                    artifact_kind="document",
                    media_type="application/pdf",
                    blob_hash=str(capture["blob_hash"]),
                    filename=str(capture["filename"]),
                    page_count=int(capture["page_count"]),
                    source_sheet_id=sheet,
                    source_row_id=row,
                    source_column_id=column,
                )["id"]
            )
        positions = {
            "engine": capture["ocr_action_params"]["engine"],
            "ocr_action_params": dict(capture["ocr_action_params"]),
            "width": page.get("width"),
            "height": page.get("height"),
            "blocks": list(page.get("blocks") or []),
            "source_artifact_id": artifact_id,
            "source_page_number": int(capture["page_number"]),
        }
        draft = PreparedPageDraft(
            int(capture["page_number"]), str(page.get("text") or ""), positions
        )
        store = PreparedContentStore(project)
        base_ref = selection.provenance_ref_id if selection is not None else None
        selected_page = document_cell(
            project, sheet_id=sheet, column_id=column, row_id=row
        ).page
        if base_ref is None:
            ref = store.stage_reference(
                source_artifact_id=artifact_id,
                producing_op_id=op_id,
                pages=[draft],
                page_number=selected_page,
                allow_partial=True,
            )
        else:
            ref = store.stage_replacement(
                base_ref_id=base_ref,
                producing_op_id=op_id,
                replacements=[draft],
                page_number=selected_page,
                allow_partial=True,
            )
        runs = RunResultStore(project)
        run_id = runs.start_run(
            op_id,
            sheet,
            "media.ocr",
            params=dict(capture["ocr_action_params"]),
            row_ids=[row],
            total_rows=1,
            commit=False,
        )
        if not receipt_store.update_body_status(
            receipt.model_copy(update={"run_id": run_id}),
            require_status="running",
            commit=False,
        ):
            raise RuntimeError("OCR receipt changed before publication")
        runs.attach_unscoped_model_calls(
            run_id,
            [
                str(call["id"])
                for call in runs.model_calls(receipt_id=receipt_id)
                if call["attempt_id"] is None and call["epoch_id"] is None
            ],
            receipt_id=receipt_id,
            action_kind="media.ocr",
            params_hash=receipt.params_hash,
            commit=False,
        )
        token = f"output-claim:{receipt_id}"
        claims, conflict = OutputColumnClaimStore(project).acquire(
            sheet_id=sheet,
            output_names=[name],
            action_kind="media.ocr",
            receipt_id=receipt_id,
            run_id=run_id,
            op_id=op_id,
            claim_token=token,
            details={"output_fields": [{"name": name, "column_type": "text"}]},
            commit=False,
        )
        if conflict is not None or len(claims) != 1:
            raise ValueError("document text is being updated")
        generations = ResultGenerationStore(project)
        generations.declare(
            run_id,
            output_column,
            output_role=name,
            compatibility_key=_compatibility_key(
                field={"name": name, "column_type": "text"}
            ),
            write_mode="create" if created else "replace_scope",
            claim_token=token,
            commit=False,
        )
        runs.write_precomputed_results(
            run_id,
            [
                {
                    "row_id": row,
                    "column_id": output_column,
                    "value": ref,
                    "publication_effect": PUBLISH_VALUE,
                }
            ],
            receipt_id=receipt_id,
            action_kind="media.ocr",
            params_hash=receipt.params_hash,
            claim_token=token,
            commit=False,
        )
        spans = []
        members = project.db.execute(
            "SELECT ref.set_id, member.page_number, member.version_id, substr(version.prepared_text,1,12000), version.positions_json "
            "FROM prepared_content_refs ref JOIN prepared_content_set_pages member ON member.set_id=ref.set_id "
            "JOIN prepared_page_versions version ON version.id=member.version_id "
            "WHERE ref.id=? AND (ref.page_number IS NULL OR ref.page_number=member.page_number) ORDER BY member.page_number",
            (ref.ref_id,),
        )
        for member in members:
            number = int(member[1])
            selector = {
                "prepared_ref_id": ref.ref_id,
                "prepared_set_id": int(member[0]),
                "prepared_version_id": int(member[2]),
                "page_number": number,
            }
            geometry = json.loads(member[4]) if member[4] else {}
            engine = geometry.get("engine") or capture["ocr_action_params"]["engine"]
            summary = record_source_span(
                project,
                artifact_id=artifact_id,
                span_kind="page_range",
                page_start=number,
                page_end=number,
                selector=selector,
                snippet=str(member[3] or "") or f"Page {number}",
                metadata={"engine": engine},
            )
            spans.append({"span_id": int(summary["id"]), "rank": len(spans)})
            for block in geometry.get("blocks") or ():
                if not isinstance(block, dict):
                    continue
                text = str(block.get("text") or "").strip()
                bbox = normalize_bbox(
                    block.get("bbox"),
                    frame="page",
                    width=geometry.get("width"),
                    height=geometry.get("height"),
                )
                if bbox is None and not text:
                    continue
                region = record_source_span(
                    project,
                    artifact_id=artifact_id,
                    span_kind="region" if bbox is not None else "page_range",
                    page_start=number,
                    page_end=number,
                    bbox=[bbox] if bbox is not None else None,
                    selector={**selector, "engine": engine},
                    quote=text or None,
                    snippet=text or None,
                )
                spans.append({"span_id": int(region["id"]), "rank": len(spans)})
        record_evidence_link(
            project,
            subject_kind="cell",
            subject_ref={
                "kind": "run_result",
                "run_id": run_id,
                "op_id": op_id,
                "row_id": row,
                "column_id": output_column,
                "prepared_ref_id": ref.ref_id,
            },
            spans=spans,
            sheet_id=sheet,
            row_id=row,
            column_id=output_column,
            run_id=run_id,
            op_id=op_id,
            receipt_id=receipt_id,
            link_role="media_ocr_grounding",
            producer={
                "action_kind": "media.ocr",
                "engine": capture["ocr_action_params"]["engine"],
            },
        )
        generations.seal(
            run_id,
            [output_column],
            claim_token=token,
            terminal_disposition="completed",
            commit=False,
        )
        runs.finish_run(run_id, "completed", commit=False)
        OutputColumnClaimStore(project).release(claim_token=token, commit=False)
    return {
        "operation_id": op_id,
        "run_id": run_id,
        "prepared_ref_id": ref.ref_id,
        "source_artifact_id": artifact_id,
        "text_column_id": output_column,
    }


def publish_cached_pdf_ocr_sample(
    project: Any, capture: Mapping[str, Any], *, prepared_ref_id: int, project_id: str
) -> dict[str, Any]:
    """Bind an already-accounted original page to another source occurrence."""
    import hashlib
    import uuid

    from frisket.contracts.action import Receipt

    with project.transaction_scope():
        row = project.db.execute(
            "SELECT version.prepared_text,version.positions_json,artifact.blob_hash "
            "FROM prepared_content_refs ref JOIN prepared_content_sets content ON content.id=ref.set_id "
            "JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id "
            "JOIN prepared_content_set_pages member ON member.set_id=ref.set_id AND member.page_number=? "
            "JOIN prepared_page_versions version ON version.id=member.version_id "
            "WHERE ref.id=? AND (ref.page_number IS NULL OR ref.page_number=member.page_number)",
            (int(capture["page_number"]), prepared_ref_id),
        ).fetchone()
        if row is None or str(row[2]).removeprefix("sha256:") != str(
            capture["blob_hash"]
        ).removeprefix("sha256:"):
            raise ValueError("cached OCR does not match the admitted source page")
        positions = json.loads(row[1]) if row[1] else {}
        if positions.get("ocr_action_params") != dict(capture["ocr_action_params"]):
            raise ValueError("cached OCR uses different settings")
        receipt_id = "receipt-" + uuid.uuid4().hex
        params_hash = (
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    dict(capture["ocr_action_params"]),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
        )
        receipts = ReceiptStore(project)
        receipts.insert_running(
            Receipt(
                receipt_id=receipt_id,
                project_id=project_id,
                action_id="action-" + uuid.uuid4().hex,
                action_kind="media.ocr",
                params_hash=params_hash,
                status="running",
            ),
            commit=False,
        )
        result = publish_pdf_ocr_sample(
            project,
            capture,
            page={
                "page": int(capture["page_number"]),
                "text": str(row[0]),
                "blocks": positions.get("blocks") or [],
                "width": positions.get("width"),
                "height": positions.get("height"),
            },
            receipt_id=receipt_id,
        )
        receipt = receipts.parsed_by_id(receipt_id)
        if receipt is None or not receipts.update_body_status(
            receipt.model_copy(update={"status": "completed"}),
            require_status="running",
            commit=False,
        ):
            raise RuntimeError("cached OCR receipt was lost")
        return {**result, "receipt_id": receipt_id}
