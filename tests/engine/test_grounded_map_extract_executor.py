from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from executor_harness import run_action_with_confirmation
from frisket.engine.store.evidence import list_cell_evidence, resolve_evidence_viewer
from frisket.ai.llm import LLMRequest, LLMResponse, ModelRouter
from frisket.engine.store.media_blobs import (
    media_cell,
    owned_media_metadata_document,
)
from frisket.server.app import create_app
from frisket.engine.store import Project
from tests.engine.extract_typed_chain_helpers import typed_extract_request


PROJECT_ID = "project-grounded-map-extract"


class _StubAdapter:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.requests: list[LLMRequest] = []

    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        self.requests.append(req)
        return LLMResponse(
            content=json.dumps(self.reply),
            data=json.loads(json.dumps(self.reply)),
            tokens_in=97,
            tokens_out=43,
            cost=0.008,
            model=req.model,
        )


class _PreviewAdapter:
    async def complete(self, req: LLMRequest, client) -> LLMResponse:  # noqa: ANN001
        field_schema = (
            (req.schema or {}).get("properties", {}).get("contract_amount", {})
        )
        if field_schema.get("type") == "object":
            data = {"contract_amount": _grounded_reply()["contract_amount"]}
        else:
            data = {"contract_amount": "$1,250,000"}
        return LLMResponse(
            content=json.dumps(data),
            data=data,
            tokens_in=20,
            tokens_out=10,
            cost=0.0,
            model=req.model,
        )


def _stub_router(reply: dict[str, Any]) -> tuple[ModelRouter, _StubAdapter]:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    adapter = _StubAdapter(reply)
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    return router, adapter


def _preview_client(tmp_path: Path) -> TestClient:
    router = ModelRouter(keys={"anthropic": "k"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = _PreviewAdapter()  # noqa: SLF001
    return TestClient(create_app(tmp_path / "workspace", router=router))


def _table_count(project: Project, table: str) -> int:
    return int(project.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _seed_document_project(tmp_path: Path, name: str) -> tuple[Path, int, int]:
    project_path = tmp_path / f"{name}.frisket"
    project = Project.create(project_path, name=name)
    try:
        sheet_id = project.add_sheet("Documents")
        filename_column_id = project.add_column(sheet_id, "filename", "text")
        document_column_id = project.add_column(sheet_id, "document", "file")
        # Model FILE inputs require an explicit To markdown / OCR step first;
        # the model reads the converted TEXT column while the file column
        # stays the grounding document (``source_document_columns``). This
        # column is that conversion's output for the seeded page text.
        document_text_column_id = project.add_column(sheet_id, "document_text", "text")
        pdf_hash = project.add_blob(
            b"%PDF-1.4 public contract amount $1,250,000",
            "contract.pdf",
            "application/pdf",
            metadata=owned_media_metadata_document(
                probe={"kind": "pdf", "pages": 4},
                owner={
                    "page_images": {
                        "3": {
                            "blob_hash": project.add_blob(
                                b"fake page image", "contract-p3.png", "image/png"
                            ),
                            "width": 1200,
                            "height": 1600,
                        }
                    },
                    "text_pages": {
                        "3": "The total contract amount is $1,250,000 payable in March."
                    },
                },
            ),
        )
        row_id = project.add_rows(
            sheet_id,
            [
                {
                    "filename": "contract.pdf",
                    "document": media_cell(
                        pdf_hash, mime="application/pdf", filename="contract.pdf"
                    ),
                    "document_text": (
                        "The total contract amount is $1,250,000 payable in March."
                    ),
                }
            ],
            {
                "filename": filename_column_id,
                "document": document_column_id,
                "document_text": document_text_column_id,
            },
        )[0]
        return project_path, sheet_id, row_id
    finally:
        project.close()


def _seed_http_document_project(client: TestClient) -> tuple[str, int]:
    project_id = client.post("/api/projects", json={"name": "Grounded Preview"}).json()[
        "id"
    ]
    project = client.app.state.workspace.get(project_id)
    sheet_id = project.add_sheet("Documents")
    cols = {"snippet": project.add_column(sheet_id, "snippet", "text")}
    project.add_rows(
        sheet_id,
        [
            {"snippet": "The total contract amount is $1,250,000."},
            {"snippet": "Vendor: Acme Civic Works."},
        ],
        cols,
    )
    return project_id, sheet_id


def _map_extract_action(
    sheet_id: int,
    *,
    key: str,
    citation_required: bool = False,
) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        # The converted text is the model input; the file column is the
        # grounding document only (never a direct model input).
        source=["filename", "document_text"],
        context="Rows are source documents.",
        instruction="Extract the contract amount and vendor.",
        fields=[
            {
                "name": "contract_amount",
                "type": "text",
                "description": "Contract amount as written.",
            },
            {
                "name": "vendor",
                "type": "text",
                "description": "Vendor name.",
            },
        ],
        grounding={
            "enabled": True,
            "allowed_methods": [
                "model_bbox",
                "exact_quote",
                "file_page_range",
            ],
        },
        source_document_columns=["document"],
        evidence_policy={"citation_required": citation_required},
        idempotency_key=key,
    )


def _single_field_grounded_action(sheet_id: int) -> dict[str, Any]:
    return typed_extract_request(
        sheet_id,
        source=["snippet"],
        instruction="Extract the contract amount.",
        fields=[
            {
                "name": "contract_amount",
                "type": "text",
                "description": "Contract amount as written.",
            }
        ],
        grounding={
            "enabled": True,
            "allowed_methods": ["model_bbox", "exact_quote"],
        },
        source_document_columns=["snippet"],
        evidence_policy={"citation_required": False},
        idempotency_key="map_extract@sha256:grounded-preview",
    )


def _grounded_reply() -> dict[str, Any]:
    return {
        "contract_amount": {
            "value": "$1,250,000",
            "evidence": [
                {
                    "kind": "source_span",
                    "span_kind": "region",
                    "page": 3,
                    "bbox": {
                        "space": "page_normalized",
                        "x0": 0.57,
                        "y0": 0.40,
                        "x1": 0.73,
                        "y1": 0.45,
                    },
                    "quote": "$1,250,000",
                    "snippet": "total contract amount is $1,250,000",
                    "grounding_method": "model_bbox",
                }
            ],
        },
        "vendor": {
            "value": "Acme Civic Works",
            "warnings": ["evidence_missing"],
        },
    }


def _grounded_bbox_spaces_reply() -> dict[str, Any]:
    return {
        "contract_amount": {
            "value": "$1,250,000",
            "evidence": [
                {
                    "kind": "source_span",
                    "span_kind": "region",
                    "page": 3,
                    "bbox": {
                        "space": "pixel",
                        "x0": 684,
                        "y0": 640,
                        "x1": 876,
                        "y1": 720,
                        "page_width": 1200,
                        "page_height": 1600,
                    },
                    "quote": "$1,250,000",
                    "snippet": "total contract amount is $1,250,000",
                    "grounding_method": "model_bbox",
                }
            ],
        },
        "vendor": {
            "value": "Acme Civic Works",
            "evidence": [
                {
                    "kind": "source_span",
                    "span_kind": "region",
                    "page": 3,
                    "bbox": {
                        "space": "page_1000",
                        "x0": 120,
                        "y0": 210,
                        "x1": 430,
                        "y1": 260,
                    },
                    "quote": "Acme Civic Works",
                    "snippet": "Vendor: Acme Civic Works",
                    "grounding_method": "model_bbox",
                }
            ],
        },
    }


def _cell_edit_action(
    *,
    row_id: int,
    column_id: int,
    value: Any,
    key: str = "cell_edit@sha256:grounded-map-extract",
) -> dict[str, Any]:
    return {
        "action_id": "cell.edit",
        "scope": {"kind": "project"},
        "params": {
            "edits": [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": value,
                }
            ]
        },
        "idempotency_key": key,
    }


def _receipt(project: Project, receipt_id: str) -> dict[str, Any]:
    row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    assert row is not None
    return json.loads(row["body"])


def _grounding_facts(receipt: dict[str, Any]) -> dict[str, Any]:
    """The run's grounding facts, whichever way the writer chunked them.

    The typed evidence writer records one ``map_extract_grounding_links``
    fact per published field; the run-level contract this file pins is the
    union of their link refs and unsupported values (in publication order).
    """
    refs = [
        item["ref"]
        for item in receipt["evidence"]
        if item["ref"]["kind"] == "map_extract_grounding_links"
    ]
    assert refs, "no grounding facts were recorded"
    merged: dict[str, Any] = {"link_refs": [], "unsupported_values": []}
    for ref in refs:
        merged["link_refs"].extend(ref["link_refs"])
        merged["unsupported_values"].extend(ref["unsupported_values"])
        for key in ref:
            if key not in ("link_refs", "unsupported_values"):
                merged.setdefault(key, ref[key])
    return merged


def test_grounded_map_extract_estimate_accepts_params_without_writes(
    tmp_path: Path,
) -> None:
    # Prompt-sample is retired because side-effect-free Preview supersedes it.
    # This now pins the estimate
    # previewer's side-effect-free acceptance of grounded map.extract params.
    client = _preview_client(tmp_path)
    project_id, sheet_id = _seed_http_document_project(client)
    project = client.app.state.workspace.get(project_id)
    action = _single_field_grounded_action(sheet_id)

    estimate = client.post(
        f"/api/projects/{project_id}/actions/v1/estimate",
        json={"action": action},
    )

    assert estimate.status_code == 200, estimate.text
    assert estimate.json()["action"]["kind"] == "map.extract"
    assert _table_count(project, "runs") == 0
    assert _table_count(project, "results") == 0
    assert _table_count(project, "source_artifacts") == 0
    assert _table_count(project, "source_spans") == 0
    assert _table_count(project, "evidence_links") == 0


def test_grounded_map_extract_writes_canonical_evidence_and_replays(
    tmp_path: Path,
) -> None:
    from frisket.actions.system import root_action_catalog, validate_root_action

    catalog = root_action_catalog()
    entry = next(item for item in catalog.actions if item.kind == "map.extract")
    assert "grounding" in entry.input_schema["properties"]
    assert "evidence_policy" in entry.input_schema["properties"]
    assert not any(item.kind == "document.extract_grounded" for item in catalog.actions)

    project_path, sheet_id, row_id = _seed_document_project(
        tmp_path, "grounded-map-extract"
    )
    action = _map_extract_action(sheet_id, key="map_extract@sha256:grounded-optional")
    validation = validate_root_action(action)
    assert validation.ok is True, validation.error

    router, adapter = _stub_router(_grounded_reply())
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed"
        assert result.receipt_id is not None
        assert len(adapter.requests) == 1

        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
            ).fetchall()
        }
        amount_column_id = int(columns["contract_amount"]["id"])
        vendor_column_id = int(columns["vendor"]["id"])
        assert (
            project.get_values(sheet_id, amount_column_id, row_ids=[row_id])[row_id]
            == "$1,250,000"
        )
        assert (
            project.get_values(sheet_id, vendor_column_id, row_ids=[row_id])[row_id]
            == "Acme Civic Works"
        )

        evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=amount_column_id,
            project_id=PROJECT_ID,
        )
        assert evidence["stale_count"] == 0
        assert len(evidence["links"]) == 1
        link_summary = evidence["links"][0]
        assert link_summary["stable_id"].startswith("evidence_link:")
        # extract-model-bbox-verification-v1: an UNVERIFIED model_bbox with no
        # resolvable OCR word stream fails closed -> page_range, never an
        # authoritative region (PT 10.2 SILENTLY-WRONG #1).
        assert link_summary["evidence_kind"] == "page_range"
        assert link_summary["viewer_href"].endswith(
            f"/evidence/links/{link_summary['stable_id']}/viewer"
        )

        viewer = resolve_evidence_viewer(
            project, link_summary["stable_id"], project_id=PROJECT_ID
        )
        assert viewer["link"]["row_id"] == row_id
        assert viewer["link"]["column_id"] == amount_column_id
        assert viewer["link"]["run_id"] == result.run_id
        assert viewer["link"]["op_id"] == result.op_ids[0]
        assert viewer["link"]["receipt_id"] == result.receipt_id
        assert viewer["link"]["producer"]["source_action_kind"] == "map.extract"
        assert viewer["link"]["producer"]["field"] == "contract_amount"
        assert (
            viewer["artifacts"][0]["artifact_ref"]["blob"]["filename"] == "contract.pdf"
        )
        # Fail-closed downgrade: the page anchor is kept (honest coarse rung),
        # but no unverified region box is drawn, and the reason is legible.
        assert viewer["artifacts"][0]["pages"][0]["page"] == 3
        assert viewer["artifacts"][0]["pages"][0]["regions"] == []
        assert "model_bbox_unverified" in viewer["warnings"]

        vendor_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=vendor_column_id,
        )
        assert vendor_evidence["links"] == []
        assert _table_count(project, "source_spans") == 1

        receipt = _receipt(project, result.receipt_id)
        grounding = _grounding_facts(receipt)
        assert grounding["link_refs"] == [
            {
                "id": viewer["link"]["id"],
                "stable_id": viewer["link"]["stable_id"],
                "row_id": row_id,
                "column_id": amount_column_id,
                "field": "contract_amount",
            }
        ]
        assert grounding["unsupported_values"] == [
            {
                "row_id": row_id,
                "field": "vendor",
                "column_id": vendor_column_id,
                "item_index": None,
                "reason": "evidence_missing",
                "citation_required": False,
            }
        ]
        assert "artifacts" not in grounding
        before_replay = {
            table: _table_count(project, table)
            for table in (
                "source_artifacts",
                "source_spans",
                "evidence_links",
                "evidence_link_spans",
                "receipts",
                "results",
            )
        }
        first_receipt_id = result.receipt_id
    finally:
        project.close()

    replay_router, replay_adapter = _stub_router(_grounded_reply())
    project = Project(project_path)
    try:
        replay = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=replay_router,
        )
        assert replay.status == "completed"
        assert replay.receipt_id == first_receipt_id
        assert replay_adapter.requests == []
        assert {
            table: _table_count(project, table)
            for table in (
                "source_artifacts",
                "source_spans",
                "evidence_links",
                "evidence_link_spans",
                "receipts",
                "results",
            )
        } == before_replay

        edit = run_action_with_confirmation(
            project,
            _cell_edit_action(
                row_id=row_id,
                column_id=amount_column_id,
                value="$1.25 million",
            ),
            project_id=PROJECT_ID,
        )
        assert edit.status == "completed"
        stale_evidence = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=amount_column_id,
            include_stale=True,
        )
        assert stale_evidence["links"][0]["status"] == "stale"
        assert stale_evidence["stale_count"] == 1
    finally:
        project.close()


def test_grounded_extract_normalizes_provider_bbox_coordinate_spaces(
    tmp_path: Path,
) -> None:

    project_path, sheet_id, row_id = _seed_document_project(
        tmp_path, "grounded-map-extract-bbox-spaces"
    )
    action = _map_extract_action(
        sheet_id,
        key="map_extract@sha256:grounded-bbox-spaces",
    )
    router, _adapter = _stub_router(_grounded_bbox_spaces_reply())
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed"
        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
            ).fetchall()
        }
        amount_column_id = int(columns["contract_amount"]["id"])
        vendor_column_id = int(columns["vendor"]["id"])

        amount_link = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=amount_column_id,
        )["links"][0]
        amount_viewer = resolve_evidence_viewer(
            project,
            amount_link["stable_id"],
            project_id=PROJECT_ID,
        )
        # extract-model-bbox-verification-v1: both provider-space model_bbox
        # guesses (pixel + page_1000) fail closed with no OCR stream to verify
        # against -> page_range + model_bbox_unverified, NOT an authoritative
        # region (PT 10.2 SILENTLY-WRONG #1). The provider coordinate-space
        # NORMALIZATION itself is unit-covered by
        # test_ocr_grounding_spans::test_normalize_bbox_preserves_map_extract_provider_spaces;
        # this executor test now pins the trust policy, not the geometry.
        amount_page = amount_viewer["artifacts"][0]["pages"][0]
        assert amount_page["regions"] == []
        assert "model_bbox_unverified" in amount_viewer["warnings"]

        vendor_link = list_cell_evidence(
            project,
            sheet_id=sheet_id,
            row_id=row_id,
            column_id=vendor_column_id,
        )["links"][0]
        vendor_viewer = resolve_evidence_viewer(
            project,
            vendor_link["stable_id"],
            project_id=PROJECT_ID,
        )
        vendor_page = vendor_viewer["artifacts"][0]["pages"][0]
        assert vendor_page["regions"] == []
        assert "model_bbox_unverified" in vendor_viewer["warnings"]
        assert _table_count(project, "source_spans") == 2
    finally:
        project.close()


def test_grounded_map_extract_citation_required_withholds_unsupported_fields(
    tmp_path: Path,
) -> None:

    project_path, sheet_id, row_id = _seed_document_project(
        tmp_path, "grounded-map-extract-required"
    )
    action = _map_extract_action(
        sheet_id,
        key="map_extract@sha256:grounded-required",
        citation_required=True,
    )
    router, adapter = _stub_router(_grounded_reply())
    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert result.status == "completed"
        assert result.receipt_id is not None
        assert len(adapter.requests) == 1
        assert {error.code for error in result.errors} == {"evidence_required"}

        columns = {
            row["name"]: row
            for row in project.db.execute(
                "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
            ).fetchall()
        }
        amount_column_id = int(columns["contract_amount"]["id"])
        vendor_column_id = int(columns["vendor"]["id"])
        assert (
            project.get_values(sheet_id, amount_column_id, row_ids=[row_id])[row_id]
            == "$1,250,000"
        )
        assert (
            project.get_values(sheet_id, vendor_column_id, row_ids=[row_id])[row_id]
            is None
        )
        # The withheld cell is outcome=withheld_unverified (terminal, NOT a failure): the
        # reason is kept as a display message but it does not inflate failed_rows.
        withheld = project.db.execute(
            "SELECT error, outcome FROM results "
            "WHERE run_id=? AND row_id=? AND column_id=?",
            (result.run_id, row_id, vendor_column_id),
        ).fetchone()
        assert withheld["error"] == "map.extract: evidence_required"
        assert withheld["outcome"] == "withheld_unverified"
        assert (
            int(
                project.db.execute(
                    "SELECT failed_rows FROM runs WHERE id=?", (result.run_id,)
                ).fetchone()["failed_rows"]
            )
            == 0
        )
        assert (
            len(
                list_cell_evidence(
                    project,
                    sheet_id=sheet_id,
                    row_id=row_id,
                    column_id=amount_column_id,
                )["links"]
            )
            == 1
        )
        assert (
            list_cell_evidence(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=vendor_column_id,
            )["links"]
            == []
        )
        receipt = _receipt(project, result.receipt_id)
        assert _grounding_facts(receipt)["unsupported_values"] == [
            {
                "row_id": row_id,
                "field": "vendor",
                "column_id": vendor_column_id,
                "item_index": None,
                "reason": "evidence_missing",
                "citation_required": True,
            }
        ]
    finally:
        project.close()


def test_grounded_map_extract_rolls_back_evidence_when_receipt_finalization_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from frisket.engine.store.receipts import ReceiptStore

    project_path, sheet_id, _row_id = _seed_document_project(
        tmp_path, "grounded-map-extract-rollback"
    )
    action = _map_extract_action(
        sheet_id,
        key="map_extract@sha256:grounded-rollback",
    )
    router, _adapter = _stub_router(_grounded_reply())
    # The grounding-links evidence is recorded into the receipt AFTER the
    # evidence writer has stored links in the same transaction; failing on that
    # exact record forces the post-grounding rollback this test asserts. The
    # executor must settle that writer failure as a terminal
    # ``project_write_failed`` result (reservation released), never leak it.
    original_record = ReceiptStore._record_writer_evidence

    def fail_after_grounding(self, ref, **kwargs):  # noqa: ANN001, ANN003, ANN202
        if ref.get("kind") == "map_extract_grounding_links":
            assert (
                self.db.execute("SELECT COUNT(*) FROM evidence_links").fetchone()[0] > 0
            )
            raise RuntimeError("forced receipt failure after grounding")
        return original_record(self, ref, **kwargs)

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", fail_after_grounding)

    project = Project(project_path)
    try:
        result = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        # Settlement contract: the paid work stays accounted under a durable
        # FAILED receipt (no repurchase on replay), while everything the batch
        # published — results, heads, artifacts, spans, links — is rolled back
        # and the output generations are sealed.
        assert result.status == "failed"
        assert [error.code for error in result.errors] == ["project_write_failed"]
        assert result.receipt_id is not None
        assert result.run_id is not None
        assert len(_adapter.requests) == 1
        run = project.db.execute("SELECT * FROM runs").fetchone()
        assert run["status"] == "failed"
        assert run["current_attempt_id"] is None
        assert _table_count(project, "receipts") == 1
        assert (
            project.db.execute("SELECT status FROM receipts").fetchone()[0] == "failed"
        )
        assert _table_count(project, "model_calls") == 1
        assert {
            str(row[0])
            for row in project.db.execute("SELECT state FROM run_output_generations")
        } == {"sealed"}
        for table in (
            "results",
            "cell_result_heads",
            "source_artifacts",
            "source_spans",
            "evidence_links",
            "evidence_link_spans",
        ):
            assert _table_count(project, table) == 0, table

        # Same key, writer healthy again: the failed receipt is replayed as-is
        # and no second paid call is made.
        monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", original_record)
        before = tuple(project.db.iterdump())
        replay = run_action_with_confirmation(
            project,
            action,
            project_id=PROJECT_ID,
            router=router,
        )
        assert replay.model_dump() == result.model_dump()
        assert len(_adapter.requests) == 1
        assert tuple(project.db.iterdump()) == before
    finally:
        project.close()
