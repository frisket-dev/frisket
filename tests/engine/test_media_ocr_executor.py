from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from executor_harness import (
    CatalogEntry,
    ExecutorCase,
    Gate,
    Reservation,
    case_env,
)
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x04\x00\x00\x00\xb5\x1c\x0c\x02"
    b"\x00\x00\x00\x0bIDATx\xdac\xfc\xff\x1f\x00\x03\x03"
    b"\x02\x00\xef\xbf\xa7\xdb\x00\x00\x00\x00IEND\xaeB`\x82"
)

# Reset by _patch_ocr_engine at the start of every harness test for this case;
# each entry is a pipeline stage crossing ("pages" or "ocr").
_OCR_CALLS: list[str] = []


def _patch_ocr_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    from frisket.ops import ocr_engines_local as ocr_local
    from frisket.ops.ocr_engines import OcrEngines

    _OCR_CALLS.clear()

    async def fake_page_images(
        self: OcrEngines,
        path: Path,
        media: Any,
        spec: dict[str, Any],
        scratch: Path,
    ) -> list[Path]:
        del self, media, scratch
        _OCR_CALLS.append("pages")
        assert spec.get("dpi") == 180
        return [path]

    async def fake_rapidocr(
        self: OcrEngines,
        page_paths: list[Path],
        scratch: Path,
        language: str | None,
    ) -> list[dict[str, Any]]:
        del self, scratch
        _OCR_CALLS.append("ocr")
        assert language == "en"
        # The page path is the materialized blob, named by its hash — echo it
        # so check_state can pin per-row routing without sharing state.
        return [
            {
                "text": f"visible text {page_paths[0].name}",
                "blocks": [
                    {
                        "text": f"block {page_paths[0].name}",
                        "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                        "score": 0.99,
                    }
                ],
            }
        ]

    monkeypatch.setattr(OcrEngines, "_page_images", fake_page_images)
    monkeypatch.setattr(OcrEngines, "_ocr_rapidocr", fake_rapidocr)
    monkeypatch.setattr(
        ocr_local, "_rapidocr_model_root_dir", lambda language=None: None
    )


def _ocr_action(
    *,
    sheet_id: int,
    row_ids: list[int] | None = None,
    input_columns: list[str] | None = None,
    engine: str = "rapidocr",
    output_name: str = "ocr_text",
    language: str | None = "en",
    dpi: int | None = 180,
    idempotency_key: str = "media_ocr@sha256:first",
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "source": (input_columns or ["media"])[0],
        "engine": engine,
    }
    if input_columns is not None and len(input_columns) != 1:
        params["source"] = input_columns
    if language is not None:
        params["language"] = language
    if dpi is not None:
        params["dpi"] = dpi
    scope = {"kind": "sheet_rows", "sheet_id": sheet_id}
    if row_ids is not None:
        scope["row_ids"] = row_ids
    return {
        "action_id": "media.ocr",
        "scope": scope,
        "params": params,
        "output_names": {"text": output_name, "blocks": output_name + "_blocks"},
        "idempotency_key": idempotency_key,
    }


def _seed(project: Project, tmp_path: Path) -> dict[str, Any]:
    del tmp_path
    sheet_id = project.add_sheet("Images")
    cols = {
        "title": project.add_column(sheet_id, "title", type="text"),
        "media": project.add_column(sheet_id, "media", type="image"),
    }
    blobs = [
        project.add_blob(
            PNG_1X1 + label.encode("ascii"),
            filename=f"{label}.png",
            mime="image/png",
            source_url=f"https://cdn.example/{label}.png",
            metadata=owned_media_metadata_document(
                probe={"width": 1, "height": 1, "kind": "image"}
            ),
        )
        for label in ("scan1", "scan2")
    ]
    row_ids = project.add_rows(
        sheet_id,
        [
            {
                "title": f"Scan {index + 1}",
                "media": media_cell(
                    blob,
                    mime="image/png",
                    filename=f"scan{index + 1}.png",
                ),
            }
            for index, blob in enumerate(blobs)
        ],
        cols,
    )
    return {"sheet_id": sheet_id, "row_ids": row_ids, "blobs": blobs}


def _make_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _ocr_action(sheet_id=seeded["sheet_id"], row_ids=seeded["row_ids"])


def _missing_source_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _ocr_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["missing"],
        idempotency_key="media_ocr@sha256:missing-cap",
    )


def _multi_input_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _ocr_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        input_columns=["media", "title"],
        idempotency_key="media_ocr@sha256:multi-input",
    )


def _bad_engine_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _ocr_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        engine="not_a_real_engine",
        idempotency_key="media_ocr@sha256:bad-engine",
    )


def _source_collision_action(seeded: dict[str, Any]) -> dict[str, Any]:
    return _ocr_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        output_name="title",
        idempotency_key="media_ocr@sha256:collision",
    )


def _columns(project: Project, sheet_id: int) -> dict[str, Any]:
    return {
        str(column["name"]): column
        for column in project.db.execute(
            "SELECT * FROM columns WHERE sheet_id=?", (sheet_id,)
        ).fetchall()
    }


def _check_state(project: Project, seeded: dict[str, Any], result: Any) -> None:
    from frisket.contracts.action import Receipt

    sheet_id = seeded["sheet_id"]
    row_ids = seeded["row_ids"]
    blobs = seeded["blobs"]
    assert _OCR_CALLS == ["pages", "ocr", "pages", "ocr"]
    assert {output.name for output in result.outputs} == {
        "ocr_text",
        "ocr_text_blocks",
    }

    columns = _columns(project, sheet_id)
    assert columns["ocr_text"]["type"] == "text"
    assert columns["ocr_text_blocks"]["type"] == "json"
    assert columns["ocr_text"]["current_run_id"] == result.run_id
    text_values = project.get_values(
        sheet_id, int(columns["ocr_text"]["id"]), row_ids=row_ids
    )
    assert text_values == {
        row_ids[0]: f"visible text {blobs[0]}",
        row_ids[1]: f"visible text {blobs[1]}",
    }
    block_values = project.get_values(
        sheet_id, int(columns["ocr_text_blocks"]["id"]), row_ids=[row_ids[0]]
    )
    assert block_values[row_ids[0]] == [
        {
            "page": 1,
            "engine": "rapidocr",
            "blocks": [
                {
                    "text": f"block {blobs[0]}",
                    "bbox": [[0, 0], [1, 0], [1, 1], [0, 1]],
                    "score": 0.99,
                }
            ],
        }
    ]
    # routed OCR made OCR a routed capability (``_ROUTED_FACT_CAPABILITIES``), so an
    # OCR run now owes the ledger one fact per row — the same shape the
    # transcription case has asserted since route resolution. Before routed OCR this asserted no
    # facts at all; a regression back to zero is the "$0 for a paid run"
    # defect, because ``price_book.settle()`` sums these rows by attempt_id.
    model_calls = RunResultStore(project).model_calls(result.run_id)
    assert len(model_calls) == 2
    assert {call["capability"] for call in model_calls} == {"ocr"}
    assert {call["engine"] for call in model_calls} == {"rapidocr"}
    # Every fact under a routed run carries its binding epoch; a NULL here
    # means a route-to-fact wire dropped.
    assert all(call["epoch_id"] is not None for call in model_calls)
    assert all(call["attempt_id"] is not None for call in model_calls)
    # One page per row, and pages are the METERED unit the per-page SKU
    # settles against.
    assert [json.loads(call["units"])["pages"] for call in model_calls] == [1, 1]

    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (result.receipt_id,)
    ).fetchone()
    receipt = Receipt.model_validate(json.loads(receipt_row["body"]))
    assert receipt.status == "completed"
    assert receipt.provider_use[0]["provider"] == "local"
    assert receipt.provider_use[0]["engine"] == "rapidocr"
    assert receipt.provider_use[0]["model_call_count"] == 2

    refs = [item.ref for item in receipt.inputs + receipt.outputs + receipt.evidence]
    ref_kinds = {ref["kind"] for ref in refs}
    assert {
        "source_column",
        "map_result_column",
        "media_ocr_grounding_evidence_link",
    } <= ref_kinds
    links = [ref for ref in refs if ref["kind"] == "media_ocr_grounding_evidence_link"]
    assert {ref["row_id"] for ref in links} == set(row_ids)
    assert {
        project.db.execute(
            "SELECT blob_hash FROM source_artifacts WHERE id=?", (ref["artifact_id"],)
        ).fetchone()[0]
        for ref in links
    } == set(blobs)
    output_refs = [ref for ref in refs if ref["kind"] == "map_result_column"]
    assert all(ref["value_hash"].startswith("sha256:") for ref in output_refs)
    assert {ref["name"]: ref["type"] for ref in output_refs} == {
        "ocr_text": "text",
        "ocr_text_blocks": "json",
    }


def _conflicting_output_action(seeded: dict[str, Any]) -> dict[str, Any]:
    # Same idempotency key as the primary action, different output name.
    return _ocr_action(
        sheet_id=seeded["sheet_id"],
        row_ids=seeded["row_ids"],
        output_name="different_ocr",
    )


def _go_stale(project: Project, seeded: dict[str, Any]) -> None:
    column = project.db.execute(
        "SELECT id FROM columns WHERE sheet_id=? AND name='ocr_text'",
        (seeded["sheet_id"],),
    ).fetchone()
    assert column is not None
    column_id = int(column["id"])
    project.db.execute("DELETE FROM cell_result_heads WHERE column_id=?", (column_id,))
    project.db.execute(
        "UPDATE ops SET status='discarded' WHERE id IN ("
        "SELECT run.op_id FROM runs run JOIN run_output_generations generation "
        "ON generation.run_id=run.id WHERE generation.column_id=?)",
        (column_id,),
    )
    project.db.execute(
        "DELETE FROM run_output_generations WHERE column_id=?", (column_id,)
    )
    project.db.execute("DELETE FROM columns WHERE id=?", (column_id,))
    project.db.commit()


CASES = [
    ExecutorCase(
        kind="media.ocr",
        request_style="typed",
        catalog=CatalogEntry(
            execution_mode="per_row",
            async_mode="queued",
            writes_project=True,
            receipt_policy="writes_receipt",
            required_capabilities=("project:write", "model:complete"),
            side_effects=frozenset(
                {
                    "read_input_rows",
                    "call_external_provider",
                    "create_generated_columns",
                    "write_run_results",
                    "write_model_calls",
                    "write_map_op",
                    "write_receipt",
                    "write_trace",
                }
            ),
            error_codes=frozenset(
                {
                    "invalid_input_ref",
                    "output_column_exists",
                    "external_cost_requires_confirmation",
                    "external_rows_failed",
                    "stale_replay",
                    "idempotency_conflict",
                    "idempotency_in_progress",
                }
            ),
            cost_policy_kind="external_metered",
            cost_requires_confirmation=True,
        ),
        seed=_seed,
        make_action=_make_action,
        patch=_patch_ocr_engine,
        gates=(
            Gate(
                "missing_source",
                _missing_source_action,
                "invalid_input_ref",
            ),
            Gate("invalid_input_ref", _multi_input_action, "invalid_action_request"),
            Gate("invalid_ocr_engine", _bad_engine_action, "invalid_action_request"),
            Gate(
                "output_column_exists",
                _source_collision_action,
                "output_column_exists",
            ),
        ),
        expect_counts={
            "columns": 2,
            "runs": 1,
            "results": 4,
            # One model-call fact per OCR row. Zero here would mean
            # settlement has nothing to rate.
            "model_calls": 2,
            "ops": 1,
            "receipts": 1,
        },
        check_state=_check_state,
        reservation=Reservation(
            make_conflict=_conflicting_output_action,
            go_stale=_go_stale,
        ),
    )
]


def test_media_ocr_replay_does_not_reenter_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        assert _OCR_CALLS == ["pages", "ocr", "pages", "ocr"]

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert _OCR_CALLS == ["pages", "ocr", "pages", "ocr"]


def _mutate_ocr_output_for_replay(project: Project, receipt_id: str) -> None:
    receipt_row = project.db.execute(
        "SELECT body FROM receipts WHERE id=?", (receipt_id,)
    ).fetchone()
    body = json.loads(receipt_row["body"])
    ref = next(
        item["ref"]
        for item in body["outputs"]
        if item["ref"].get("name") == "ocr_text"
        and item["ref"]["kind"] == "map_result_column"
    )
    assert ref["value_hash"].startswith("sha256:")
    project.db.execute(
        "UPDATE results SET value=? WHERE run_id=? AND row_id=? AND column_id=?",
        (
            json.dumps("structurally drifted OCR text"),
            ref["run_id"],
            ref["row_ids"][0],
            ref["column_id"],
        ),
    )
    project.db.commit()


def test_media_ocr_published_output_is_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        with pytest.raises(sqlite3.IntegrityError, match="semantics are immutable"):
            _mutate_ocr_output_for_replay(env.project, first.receipt_id)

        replay = env.run_primary()
        assert replay.status == "completed", replay.errors
        assert replay.receipt_id == first.receipt_id
        assert _OCR_CALLS == ["pages", "ocr", "pages", "ocr"]


def test_media_ocr_replay_preserves_identity_rejections_and_dual_defect_priority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from frisket.contracts.action import (
        Receipt,
    )
    from frisket.sdk.replay import output_columns_replay_error

    expected_messages = {
        "invalid_ref": "media.ocr replay receipt has invalid output refs",
        "missing": "media.ocr replay output column is missing",
        "renamed": "media.ocr replay output column was renamed",
        "type_changed": "media.ocr replay output column type changed",
        "run_changed": "media.ocr replay output column changed runs",
        "no_output": "media.ocr replay receipt has no output column ref",
    }
    with case_env(CASES[0], tmp_path, monkeypatch) as env:
        first = env.run_primary()
        assert first.status == "completed", first.errors
        receipt_row = env.project.db.execute(
            "SELECT body FROM receipts WHERE id=?", (first.receipt_id,)
        ).fetchone()
        base = json.loads(receipt_row["body"])

        for defect, expected_message in expected_messages.items():
            body = json.loads(json.dumps(base))
            if defect == "no_output":
                body["outputs"] = [
                    item
                    for item in body["outputs"]
                    if item["ref"]["kind"] != "map_result_column"
                ]
                body["evidence"].append({"ref": {"kind": "media_ocr_searchable_pdf"}})
            else:
                ref = next(
                    item["ref"]
                    for item in body["outputs"]
                    if item["ref"]["kind"] == "map_result_column"
                )
                if defect == "invalid_ref":
                    ref["column_id"] = "not-an-integer"
                elif defect == "missing":
                    ref["column_id"] = 2_147_483_647
                elif defect == "renamed":
                    ref["name"] = "renamed_ocr"
                elif defect == "type_changed":
                    ref["type"] = "integer"
                else:
                    assert defect == "run_changed"
                    ref["run_id"] += 1

            error = output_columns_replay_error(
                env.project,
                Receipt.model_validate(body),
                output_kind="map_result_column",
                action_kind="media.ocr",
                expected_row_ids=env.seeded["row_ids"],
            )
            assert error is not None, defect
            assert error.code == "stale_replay", defect
            assert error.message == expected_message, defect


def test_searchable_pdf_false_default_is_normalized_for_idempotency() -> None:
    from frisket.actions.system import typed_action_for_request
    from frisket.engine.executor.map_rows_action import typed_request_hash

    omitted = _ocr_action(sheet_id=1)
    explicit = _ocr_action(sheet_id=1)
    explicit["params"]["searchable_pdf"] = False
    enabled = _ocr_action(sheet_id=1)
    enabled["params"]["searchable_pdf"] = True
    assert typed_request_hash(typed_action_for_request(omitted)) == typed_request_hash(
        typed_action_for_request(explicit)
    )
    assert typed_request_hash(typed_action_for_request(enabled)) != typed_request_hash(
        typed_action_for_request(omitted)
    )


def test_media_ocr_params_probes() -> None:
    from frisket.actions.media import OcrParams
    from frisket.ai.llm.types import provider_from_model_id

    with pytest.raises(ValueError):
        OcrParams(source=["media"])
    assert provider_from_model_id("provider/model") == "provider"
